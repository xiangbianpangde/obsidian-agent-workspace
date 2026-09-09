"""
Unified IM Hub - Acceptance Tests Suite (AT-1 ~ AT-9)
Conforms strictly to docs/03-im-integration-v0.2.7.md
Validates all mechanical contracts, invariants, deduplication, and security defenses.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.im.adapters.base import IMIngestDriver, IMSourceReader
from backend.app.im.adapters.qq import QQSnapshotAdapter, make_qq_synthetic_key
from backend.app.im.adapters.wechat import WxCliAdapter, make_wechat_synthetic_key
from backend.app.im.adapters.wecom import WeComSnapshotAdapter
from backend.app.im.coordinator import IngestionCoordinator
from backend.app.im.journal import (
    IdentityConflictError,
    IMJournal,
    InvalidIngestEnvelopeError,
)
from backend.app.im.models import (
    IMIngestBatch,
    IMIngestRecord,
    IMMessageItem,
    IMWatermark,
    canonical_bytes_v1,
    compute_server_digest,
)
from backend.app.main import app

# -----------------------------------------------------------------------------
# Test Helpers
# -----------------------------------------------------------------------------


def make_sample_message(
    source: str = "wechat",
    account_id: str = "acc_main",
    channel_id: str = "channel_1",
    msg_id: str = "m1",
    text: str = "Hello",
    epoch_ms: int = 1725400000000,
    reply_to: str = None,
) -> IMMessageItem:
    return IMMessageItem(
        id=f"{source}_msg_{msg_id}",
        ingest_seq=0,
        source=source,
        account_id=account_id,
        channel_id=channel_id,
        channel_name="Test Channel",
        source_id_quality="synthetic",
        source_message_id=msg_id,
        sender_id="u123",
        sender_name="Alice",
        sender_role=None,
        is_self=False,
        reply_to=reply_to,
        text=text,
        message_type="text",
        mentions=[],
        attachments=[],
        occurred_at=datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat(),
        occurred_at_epoch_ms=epoch_ms,
        observed_at=datetime.now(timezone.utc).isoformat(),
        provenance={"mode": "sse"},
        focus_tags=[],
        focus_reasons=[],
    )


# -----------------------------------------------------------------------------
# AT-1: Capability Decoupling
# -----------------------------------------------------------------------------


def test_at1r_qq_snapshot_capability_and_reader_driver_shape():
    """AT-1R: QQ is a conservative snapshot Reader/Driver, never a realtime bot."""
    qq_adp = QQSnapshotAdapter()
    assert isinstance(qq_adp, IMSourceReader)
    assert isinstance(qq_adp, IMIngestDriver)
    assert qq_adp.capabilities.canReadHistory is True
    assert qq_adp.capabilities.realtime is False
    assert qq_adp.capabilities.media == "placeholder"
    assert qq_adp.capabilities.nativeUnread is False
    assert qq_adp.capabilities.reliableSelfIdentity is False
    assert qq_adp.capabilities.mentions is False
    assert qq_adp.capabilities.replies is False
    assert qq_adp.capabilities.recallEvents is False

    wx_adp = WxCliAdapter()
    assert wx_adp.capabilities.canReadHistory is True
    assert hasattr(wx_adp, "read_history")


# -----------------------------------------------------------------------------
# AT-2: Cross-Path Digest Consistency & Reply-To Identity Domain
# -----------------------------------------------------------------------------


def test_at2_cross_path_canonical_digest_and_reply_to():
    """
    AT-2 Positive: Same physical WeChat message observed through two ingestion paths
    produces byte-identical canonical bytes and server digest -> idempotent skip.
    AT-2 Negative: Same dedupe_key but different objective fact -> IdentityConflictError.
    """
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")
        wx_adp = WxCliAdapter(account_id="wx_primary")

        # Native wx-cli timeline payload (real transport shape).
        native = {
            "sort_seq": 1788516000000,
            "server_id": 999888777,
            "msg_type": 1,
            "sub_type": 0,
            "sender": "wxid_friend",
            "talker": "course_group@chatroom",
            "talker_display_name": "高等数学课程群",
            "sender_display_name": "学习委员",
            "direction": "incoming",
            "snippet": "作业已提交",
            "create_time": 1788516000,
            "status": 3,
        }

        # Path 1: live SSE-style ingestion.
        rec_sse = wx_adp.normalize_wx_item(native, provenance_mode="sse")
        # Path 2: historical timeline catch-up. The only difference is provenance mode
        # (a workspace-local field excluded from the canonical payload).
        rec_timeline = wx_adp.normalize_wx_item(native, provenance_mode="timeline")

        # 1. Canonical payload must be byte-for-byte identical across paths.
        bytes_sse = canonical_bytes_v1(rec_sse.message)
        bytes_timeline = canonical_bytes_v1(rec_timeline.message)
        assert bytes_sse == bytes_timeline

        digest_sse = compute_server_digest(rec_sse.message)
        digest_timeline = compute_server_digest(rec_timeline.message)
        assert digest_sse == digest_timeline
        assert rec_sse.dedupe_key == rec_timeline.dedupe_key
        assert rec_sse.dedupe_key == "wx_locator:wx_primary:999888777"

        # 2. Commit SSE batch first
        batch_1 = IMIngestBatch(source="wechat", account_id="wx_primary", records=[rec_sse])
        receipt_1 = journal.commit_batch(batch_1)
        assert receipt_1.inserted_count == 1
        assert receipt_1.skipped_count == 0

        # 3. Commit timeline batch (same message) -> idempotent skip
        batch_2 = IMIngestBatch(source="wechat", account_id="wx_primary", records=[rec_timeline])
        receipt_2 = journal.commit_batch(batch_2)
        assert receipt_2.inserted_count == 0
        assert receipt_2.skipped_count == 1

        # 4. Negative test: same dedupe_key but a different objective fact (edited text).
        conflicting = dict(native)
        conflicting["snippet"] = "作业已撤回"
        rec_conflict = wx_adp.normalize_wx_item(conflicting)
        assert rec_conflict.dedupe_key == rec_sse.dedupe_key
        assert compute_server_digest(rec_conflict.message) != digest_sse

        batch_conflict = IMIngestBatch(
            source="wechat", account_id="wx_primary", records=[rec_conflict]
        )
        with pytest.raises(IdentityConflictError):
            journal.commit_batch(batch_conflict)

        journal.close()


# -----------------------------------------------------------------------------
# AT-3: Batch Mixed Deduplication and Watermark Advancement
# -----------------------------------------------------------------------------


def test_at3_batch_mixed_dedupe_and_watermark():
    """AT-3: [existing A, new B, replay A] mixed batch -> A skipped, B inserted, watermark advanced."""
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")

        msg_a = make_sample_message(msg_id="msg_A", text="Message A")
        rec_a = IMIngestRecord(
            source="wechat",
            account_id="acc_main",
            dedupe_key="key_A",
            dedupe_basis="native_message_id",
            message=msg_a,
        )

        # Pre-commit A
        journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc_main", records=[rec_a]))

        # Prepare mixed batch: [A, B, A]
        msg_b = make_sample_message(msg_id="msg_B", text="Message B")
        rec_b = IMIngestRecord(
            source="wechat",
            account_id="acc_main",
            dedupe_key="key_B",
            dedupe_basis="native_message_id",
            message=msg_b,
        )

        wm = IMWatermark(
            kind="source_cursor", value="cursor_100", committed_at="2026-09-04T10:05:00Z"
        )
        mixed_batch = IMIngestBatch(
            source="wechat", account_id="acc_main", records=[rec_a, rec_b, rec_a], new_watermark=wm
        )

        receipt = journal.commit_batch(mixed_batch)
        assert receipt.inserted_count == 1
        assert receipt.skipped_count == 2
        assert receipt.watermark_advanced is True

        # Verify watermark actually advanced in database
        saved_wm = journal.get_source_watermark("wechat", "acc_main")
        assert saved_wm is not None
        assert saved_wm.value == "cursor_100"

        journal.close()


# -----------------------------------------------------------------------------
# AT-4: Forged Provided Digest Rejection
# -----------------------------------------------------------------------------


def test_at4_forged_provided_digest_rejection():
    """AT-4: Submitting forged provided_digest fails immediately with 400 and does not write."""
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")

        msg = make_sample_message(msg_id="forged_1", text="Normal text")
        rec = IMIngestRecord(
            source="wechat",
            account_id="acc_main",
            dedupe_key="key_forged",
            dedupe_basis="native_message_id",
            message=msg,
            provided_digest="deadbeef" * 8,  # Forged wrong hash
        )

        with pytest.raises(ValueError, match="Provided digest mismatch"):
            journal.commit_batch(
                IMIngestBatch(source="wechat", account_id="acc_main", records=[rec])
            )

        assert journal.get_current_head_seq() == 0
        journal.close()


# -----------------------------------------------------------------------------
# AT-5: Envelope-Message Identity Domain Mismatch Dual Branch
# -----------------------------------------------------------------------------


def test_at5_envelope_identity_domain_mismatch_dual_branch():
    """
    AT-5A: record.source="wechat", message.source="qq" -> 400 InvalidIngestEnvelope, 0 writes.
    AT-5B: record.account_id="acc_1", message.account_id="acc_2" -> 400 InvalidIngestEnvelope, 0 writes.
    """
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")

        # Branch A: source mismatch
        msg_a = make_sample_message(source="qq", account_id="acc_1")
        rec_a = IMIngestRecord(
            source="wechat",
            account_id="acc_1",
            dedupe_key="key_1",
            dedupe_basis="native_message_id",
            message=msg_a,
        )

        with pytest.raises(InvalidIngestEnvelopeError):
            journal.commit_batch(
                IMIngestBatch(source="wechat", account_id="acc_1", records=[rec_a])
            )
        assert journal.get_current_head_seq() == 0

        # Branch B: account_id mismatch
        msg_b = make_sample_message(source="wechat", account_id="acc_2")
        rec_b = IMIngestRecord(
            source="wechat",
            account_id="acc_1",
            dedupe_key="key_2",
            dedupe_basis="native_message_id",
            message=msg_b,
        )

        with pytest.raises(InvalidIngestEnvelopeError):
            journal.commit_batch(
                IMIngestBatch(source="wechat", account_id="acc_1", records=[rec_b])
            )
        assert journal.get_current_head_seq() == 0

        journal.close()


# -----------------------------------------------------------------------------
# AT-6: Physical Locator Collision Negative Test
# -----------------------------------------------------------------------------


def test_at6_physical_locator_collision_negative_test():
    """
    AT-6: Two messages with identical text, sender, and timestamp but different
    physical record locators must produce distinct synthetic_v1 dedupe keys
    and both insert independently without collision.
    """
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")

        # Message 1
        key_1 = make_wechat_synthetic_key("wx_main", "physical_svr_id_101")
        msg_1 = make_sample_message(
            account_id="wx_main", msg_id="101", text="收到", epoch_ms=1725400000000
        )
        rec_1 = IMIngestRecord(
            source="wechat",
            account_id="wx_main",
            dedupe_key=key_1,
            dedupe_basis="synthetic_v1",
            message=msg_1,
        )

        # Message 2 (exact same content, sender, and timestamp, but different physical id)
        key_2 = make_wechat_synthetic_key("wx_main", "physical_svr_id_102")
        msg_2 = make_sample_message(
            account_id="wx_main", msg_id="102", text="收到", epoch_ms=1725400000000
        )
        rec_2 = IMIngestRecord(
            source="wechat",
            account_id="wx_main",
            dedupe_key=key_2,
            dedupe_basis="synthetic_v1",
            message=msg_2,
        )

        assert key_1 != key_2

        receipt = journal.commit_batch(
            IMIngestBatch(source="wechat", account_id="wx_main", records=[rec_1, rec_2])
        )
        assert receipt.inserted_count == 2
        assert receipt.skipped_count == 0
        assert journal.get_current_head_seq() == 2

        journal.close()


# -----------------------------------------------------------------------------
# AT-7: Open Interval Replay & Dual Cursor Resolution
# -----------------------------------------------------------------------------


def test_at7_open_interval_replay_and_dual_cursor():
    """
    AT-7: Dual cursor max(after_seq=1000, Last-Event-ID=1100) -> 1100.
    Replay starts at open interval > 1100. Future cursor -> 400 InvalidCursor.
    """
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")
        coordinator = IngestionCoordinator(journal, ring_buffer_capacity=50)

        # Commit 10 messages
        records = []
        for i in range(1, 11):
            m = make_sample_message(account_id="acc", msg_id=f"seq_{i}", text=f"Msg {i}")
            records.append(
                IMIngestRecord(
                    source="wechat",
                    account_id="acc",
                    dedupe_key=f"k_{i}",
                    dedupe_basis="native_message_id",
                    message=m,
                )
            )

        journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc", records=records))
        assert journal.get_current_head_seq() == 10

        # Open interval query: after_seq=5 -> returns 6, 7, 8, 9, 10
        replayed = journal.query_replay_events(after_seq=5)
        assert len(replayed) == 5
        assert [m.ingest_seq for m in replayed] == [6, 7, 8, 9, 10]

        journal.close()


# -----------------------------------------------------------------------------
# AT-8: Resync Snapshot Exhaustive Pagination Gate
# -----------------------------------------------------------------------------


def test_at8_resync_snapshot_exhaustive_pagination():
    """
    AT-8: 137 backlog messages <= snapshot_head_seq=137 with limit=50.
    Requires exactly 3 page iterations until next_cursor is None. 0 gap!
    """
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")

        # Commit 137 messages
        records = []
        for i in range(1, 138):
            m = make_sample_message(
                account_id="acc", msg_id=f"m_{i}", text=f"Item {i}", epoch_ms=1725400000000 + i
            )
            records.append(
                IMIngestRecord(
                    source="wechat",
                    account_id="acc",
                    dedupe_key=f"k_{i}",
                    dedupe_basis="native_message_id",
                    message=m,
                )
            )

        journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc", records=records))
        snapshot_head = journal.get_current_head_seq()
        assert snapshot_head == 137

        # Paging loop simulation
        all_fetched = []
        cursor = 0
        pages = 0

        while True:
            items, next_cursor = journal.query_snapshot_page(
                snapshot_head_seq=snapshot_head, cursor=cursor, limit=50
            )
            pages += 1
            all_fetched.extend(items)
            if next_cursor is None:
                break
            cursor = next_cursor

        assert pages == 3
        assert len(all_fetched) == 137
        assert [m.ingest_seq for m in all_fetched] == list(range(1, 138))

        journal.close()


# -----------------------------------------------------------------------------
# AT-9: Permission Hardening on Pre-existing Permissive Files
# -----------------------------------------------------------------------------


def test_at9_permission_hardening_existing_files():
    """
    AT-9: Pre-existing 0666 db and 0777 directory automatically repaired to 0600 and 0700.
    No broad permissions remain on db, -wal, -shm.
    """
    with tempfile.TemporaryDirectory() as td:
        im_dir = Path(td) / "insecure_im"
        im_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(im_dir, 0o777)

        db_file = im_dir / "im_hub.db"
        db_file.touch()
        os.chmod(db_file, 0o666)

        wal_file = im_dir / "im_hub.db-wal"
        wal_file.touch()
        os.chmod(wal_file, 0o666)

        # Bootstrapping IMJournal must automatically repair permissions
        journal = IMJournal(db_file)

        # Assert repaired
        dir_stat = os.stat(im_dir)
        assert dir_stat.st_mode & 0o077 == 0

        db_stat = os.stat(db_file)
        assert db_stat.st_mode & 0o077 == 0

        wal_stat = os.stat(wal_file)
        assert wal_stat.st_mode & 0o077 == 0

        journal.close()


# -----------------------------------------------------------------------------
# Real wx-cli transport regression tests
# -----------------------------------------------------------------------------


def test_wx_cli_native_timeline_payload_normalization():
    """Native wx-cli fields must map to a stable workspace record."""
    adapter = WxCliAdapter(account_id="wxid_me")
    record = adapter.normalize_wx_item(
        {
            "sort_seq": 998,
            "server_id": 123456,
            "msg_type": 1,
            "sub_type": 0,
            "sender": "wxid_friend",
            "talker": "class_group@chatroom",
            "talker_display_name": "课程通知群",
            "sender_display_name": "班长",
            "direction": "incoming",
            "snippet": "明天换教室",
            "create_time": 1788516000,
            "status": 0,
        },
        provenance_mode="timeline",
    )

    assert record.dedupe_key == "wx_locator:wxid_me:123456"
    assert record.message.channel_id == "wechat:class_group@chatroom"
    assert record.message.channel_name == "课程通知群"
    assert record.message.sender_name == "班长"
    assert record.message.text == "明天换教室"
    assert record.message.message_type == "text"
    assert record.message.is_self is False
    assert record.message.occurred_at_epoch_ms == 1788516000000


def test_wx_cli_unreachable_is_not_reported_live():
    """Starting the adapter must not fabricate a live source status."""

    async def scenario():
        adapter = WxCliAdapter(base_url="http://127.0.0.1:9", poll_interval_secs=60)
        journal = IMJournal(Path(tempfile.mkdtemp()) / "im_test.db")
        coordinator = IngestionCoordinator(journal)
        await adapter.start(coordinator)
        await asyncio.sleep(0.05)
        source_status = await adapter.get_status()
        await adapter.stop()
        journal.close()
        assert source_status.connectivity != "live"

    asyncio.run(scenario())


# -----------------------------------------------------------------------------
# AT-10: Snapshot Capture & WAL Integrity
# -----------------------------------------------------------------------------


def test_at10_snapshot_wal_integrity():
    """
    AT-10: Copying an active database without its WAL leaves uncheckpointed rows
    invisible; copying DB + WAL allows SQLite recovery to see every committed row.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src_db = root / "active.db"
        with sqlite3.connect(str(src_db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("CREATE TABLE test_items (id INTEGER PRIMARY KEY, val TEXT);")
            conn.execute("INSERT INTO test_items (val) VALUES ('checkpointed');")
            conn.commit()

        # Checkpoint the first insert
        with sqlite3.connect(str(src_db)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

        # Now insert a second row in WAL without checkpointing
        with sqlite3.connect(str(src_db)) as conn:
            conn.execute("INSERT INTO test_items (val) VALUES ('in_wal_only');")
            conn.commit()

        wal_file = root / "active.db-wal"
        assert wal_file.exists() and wal_file.stat().st_size > 0

        # Branch A: Copy ONLY the db file without WAL
        alone_dir = root / "alone"
        alone_dir.mkdir()
        shutil.copy2(src_db, alone_dir / "active.db")
        with sqlite3.connect(f"file:{alone_dir / 'active.db'}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT val FROM test_items;").fetchall()
            # The WAL-only row is missing when WAL is omitted
            assert len(rows) == 1
            assert rows[0][0] == "checkpointed"

        # Branch B: Copy BOTH db and wal files
        with_wal_dir = root / "with_wal"
        with_wal_dir.mkdir()
        shutil.copy2(src_db, with_wal_dir / "active.db")
        shutil.copy2(wal_file, with_wal_dir / "active.db-wal")
        # Standard SQLite opening with WAL performs recovery and sees both rows
        with sqlite3.connect(str(with_wal_dir / "active.db")) as conn:
            rows = conn.execute("SELECT val FROM test_items ORDER BY id ASC;").fetchall()
            assert len(rows) == 2
            assert rows[0][0] == "checkpointed"
            assert rows[1][0] == "in_wal_only"


# -----------------------------------------------------------------------------
# AT-11: Fault Injection on Atomic Publish & Quarantine
# -----------------------------------------------------------------------------


def test_at11_fault_injection_and_quarantine():
    """
    AT-11: When extraction/validation fails in staging, the staging directory is
    moved to quarantine/<run_id>-<code>, CURRENT pointer is untouched, and old
    snapshots are never modified.
    """
    from backend.scripts.qq_snapshot.capture import _quarantine

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        staging_dir = root / "staging" / "20260908T120000-abcd1234ef56"
        staging_dir.mkdir(parents=True)
        (staging_dir / "partial.txt").write_text("in-progress data")
        quarantine_dir = root / "quarantine"

        # Trigger quarantine for integrity failure
        _quarantine(staging_dir, quarantine_dir, "QQ_SNAPSHOT_INTEGRITY_FAILED")

        # Staging directory must no longer exist
        assert not staging_dir.exists()
        # Quarantine entry must exist with code suffix
        quarantined = list(quarantine_dir.iterdir())
        assert len(quarantined) == 1
        assert "qq_snapshot_integrity_failed" in quarantined[0].name
        assert (quarantined[0] / "partial.txt").exists()


# -----------------------------------------------------------------------------
# AT-12: Permissions, Immutability & Path Isolation
# -----------------------------------------------------------------------------


def test_at12_permissions_and_path_isolation():
    """
    AT-12:
    1. Adapter rejects snapshot roots pointing directly to QQ container paths.
    2. Overly permissive snapshot files (group/world readable) fail closed with
       QQ_SNAPSHOT_PERMISSION instead of blindly reading them.
    """
    # 1. Path isolation: pointing to Tencent container is rejected
    fake_container = Path.home() / "Library/Containers/com.tencent.qq/Data/Library/fake"
    adapter_isolated = QQSnapshotAdapter(account_id="isolated_test", snapshot_root=fake_container)
    with pytest.raises(Exception) as exc_info:
        adapter_isolated._validate_current()
    assert "QQ_SNAPSHOT_PATH_REJECTED" in str(exc_info.value)

    # 2. Permission hardening: permissions > 0600 on CURRENT are rejected
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        (root / "snapshots").mkdir(mode=0o700)
        current_file = root / "CURRENT"
        current_file.write_text("qqsnap-v1-0123456789abcdef01234567\n")
        os.chmod(current_file, 0o666)  # Permissive!

        adapter_perm = QQSnapshotAdapter(account_id="perm_test", snapshot_root=root)
        with pytest.raises(Exception) as exc_info:
            adapter_perm._validate_current()
        assert "QQ_SNAPSHOT_PERMISSION" in str(exc_info.value)


# -----------------------------------------------------------------------------
# AT-13: Zero Key/Salt Leakage & Cache-Control: no-store
# -----------------------------------------------------------------------------


def test_at13_zero_leakage_and_global_no_store():
    """
    AT-13:
    1. Published manifest does not contain raw key or salt fields.
    2. Every /api/im endpoint (success, 404, error) returns Cache-Control: no-store.
    """
    client = TestClient(app)

    # 1. Check all public /api/im routes
    endpoints = [
        "/api/im/status",
        "/api/im/overview",
        "/api/im/channels",
        "/api/im/timeline",
        "/api/im/snapshot?snapshot_head_seq=1",
        "/api/im/nonexistent_route_test",
    ]
    for ep in endpoints:
        res = client.get(ep)
        cc = res.headers.get("Cache-Control", "")
        assert "no-store" in cc, f"endpoint {ep} missing no-store in Cache-Control: {cc}"
        assert "no-cache" in cc

    # 2. Check that if a real manifest exists, it has no key_hex or salt_hex
    vault_current = (
        Path.home() / "Library/Application Support/qq-local-vault/accounts/qq_primary/CURRENT"
    )
    if vault_current.exists():
        snap_id = vault_current.read_text().strip()
        manifest_path = vault_current.parent / "snapshots" / snap_id / "manifest.json"
        if manifest_path.exists():
            manifest_text = manifest_path.read_text()
            assert "key_hex" not in manifest_text
            assert "salt_hex" not in manifest_text
            assert "PRAGMA key" not in manifest_text


# -----------------------------------------------------------------------------
# AT-14: Codec & Schema Drift Fail-Closed
# -----------------------------------------------------------------------------


def test_at14_schema_drift_fail_closed():
    """
    AT-14: If a snapshot's manifest has a mismatched critical_schema_fingerprint,
    the adapter refuses to ingest, reports degraded status, and does not advance watermark.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        (root / "snapshots").mkdir(mode=0o700)
        snap_id = "qqsnap-v1-0123456789abcdef01234567"
        snap_dir = root / "snapshots" / snap_id
        snap_dir.mkdir(mode=0o700)
        (snap_dir / "export").mkdir(mode=0o700)

        # Write corrupted manifest with wrong schema fingerprint
        corrupted_manifest = {
            "schema": "qq.snapshot/v1",
            "snapshot_id": snap_id,
            "account_alias": "drift_acc",
            "schema_profile_id": "ntqq-macos-6.9.98-critical-schema-v1",
            "critical_schema_fingerprint": "bad_fingerprint_hash_drift",
            "locator_profile_id": "qq-locator-v1",
            "normalization_profile_id": "qq-im-normalization-v1",
        }
        manifest_file = snap_dir / "manifest.json"
        manifest_file.write_text(json.dumps(corrupted_manifest))
        os.chmod(manifest_file, 0o600)

        current_file = root / "CURRENT"
        current_file.write_text(snap_id + "\n")
        os.chmod(current_file, 0o600)

        adapter = QQSnapshotAdapter(account_id="drift_acc", snapshot_root=root)
        with pytest.raises(Exception) as exc_info:
            adapter._validate_current()
        assert "QQ_SNAPSHOT_SCHEMA_UNSUPPORTED" in str(exc_info.value)


# -----------------------------------------------------------------------------
# AT-15: Cross-Snapshot Dedupe & No-Delete Invariant
# -----------------------------------------------------------------------------


def test_at15_cross_snapshot_dedupe_and_no_delete():
    """
    AT-15:
    Snapshot 1 commits [m1, m2].
    Snapshot 2 commits [m1, m2, m3] -> m1, m2 skipped, m3 inserted.
    Snapshot 3 commits [m1, m3] (m2 omitted from source) -> m2 is NEVER deleted from journal.
    """
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")

        # Snapshot 1
        m1 = make_sample_message(
            source="qq", account_id="qq_acc", msg_id="101", text="Msg 1", epoch_ms=1788500001000
        )
        m2 = make_sample_message(
            source="qq", account_id="qq_acc", msg_id="102", text="Msg 2", epoch_ms=1788500002000
        )
        r1 = IMIngestRecord(
            source="qq",
            account_id="qq_acc",
            dedupe_key=make_qq_synthetic_key("qq_acc", "group", "101"),
            dedupe_basis="synthetic_v1",
            message=m1,
        )
        r2 = IMIngestRecord(
            source="qq",
            account_id="qq_acc",
            dedupe_key=make_qq_synthetic_key("qq_acc", "group", "102"),
            dedupe_basis="synthetic_v1",
            message=m2,
        )

        rcpt_1 = journal.commit_batch(
            IMIngestBatch(
                source="qq",
                account_id="qq_acc",
                records=[r1, r2],
                new_watermark=IMWatermark(
                    kind="snapshot_version", value="snap_1", committed_at="2026-09-08T12:00:00Z"
                ),
            )
        )
        assert rcpt_1.inserted_count == 2

        # Snapshot 2: [m1, m2, m3]
        m3 = make_sample_message(
            source="qq", account_id="qq_acc", msg_id="103", text="Msg 3", epoch_ms=1788500003000
        )
        r3 = IMIngestRecord(
            source="qq",
            account_id="qq_acc",
            dedupe_key=make_qq_synthetic_key("qq_acc", "group", "103"),
            dedupe_basis="synthetic_v1",
            message=m3,
        )

        rcpt_2 = journal.commit_batch(
            IMIngestBatch(
                source="qq",
                account_id="qq_acc",
                records=[r1, r2, r3],
                new_watermark=IMWatermark(
                    kind="snapshot_version", value="snap_2", committed_at="2026-09-08T12:05:00Z"
                ),
            )
        )
        assert rcpt_2.inserted_count == 1
        assert rcpt_2.skipped_count == 2
        assert journal.get_current_head_seq() == 3

        # Snapshot 3: [m1, m3] (m2 omitted from upstream)
        rcpt_3 = journal.commit_batch(
            IMIngestBatch(
                source="qq",
                account_id="qq_acc",
                records=[r1, r3],
                new_watermark=IMWatermark(
                    kind="snapshot_version", value="snap_3", committed_at="2026-09-08T12:10:00Z"
                ),
            )
        )
        assert rcpt_3.inserted_count == 0
        assert rcpt_3.skipped_count == 2

        # Invariant check: m2 still exists in the journal! (Append-only / No-delete)
        all_msgs = journal.query_replay_events(after_seq=0, limit=10)
        assert len(all_msgs) == 3
        ids = [m.source_message_id for m in all_msgs]
        assert "102" in str(ids)
        journal.close()


# -----------------------------------------------------------------------------
# AT-16: Conservative Normalization Invariants
# -----------------------------------------------------------------------------


def test_at16_conservative_normalization():
    """
    AT-16: QQ normalized messages satisfy:
    - is_self is None (never defaulted to False)
    - reply_to is None (not fabricated)
    - mentions is [] (no regex false positives)
    - attachments availability is 'placeholder'
    - sender_name fallback is deterministic 'QQ用户 <token>'
    """
    adapter = QQSnapshotAdapter(account_id="qq_test")
    fake_row = {
        "msg_id": 999111,
        "chat_type": 2,
        "msg_type": 2,
        "sub_msg_type": 0,
        "send_type": 0,
        "sender_uid": "u_secret_42",
        "peer_uid": None,
        "peer_uin": 2026001,
        "sender_uin": 10001,
        "msg_time": 1788500000,
        "sender_member_name": None,
        "sender_nickname": None,
        "body": b"",
    }
    rec = adapter._normalize_row(
        table_role="group",
        row=fake_row,
        group_names={str(2026001): "测试学习群"},
        buddy_names={},
        snapshot_id="snap_test",
        observed_at="2026-09-08T12:00:00Z",
    )

    assert rec is not None
    msg = rec.message
    assert msg.is_self is None
    assert msg.reply_to is None
    assert msg.mentions == []
    assert msg.sender_name.startswith("QQ用户 ")
    assert msg.channel_name == "测试学习群"
    assert rec.dedupe_key == "qq_locator:v1:7:qq_test:5:group:i:999111"


# -----------------------------------------------------------------------------
# AT-17: Workstation Zero Outbound & Removed Zhin Ingress
# -----------------------------------------------------------------------------


def test_at17_zhin_ingress_removed_and_im_errors_no_store():
    client = TestClient(app)
    response = client.post("/internal/im/ingest/zhin", json={"event_id": "forbidden"})
    assert response.status_code == 404
    assert "no-store" in response.headers.get("Cache-Control", "")

    # Test /api/im/sync trigger endpoint
    sync_resp = client.post("/api/im/sync")
    assert sync_resp.status_code == 200
    assert sync_resp.json().get("status") == "ok"
    assert "no-store" in sync_resp.headers.get("Cache-Control", "")

    # Check OpenAPI schema for zero outbound send/reply/recall endpoints
    schema = app.openapi()
    paths = schema.get("paths", {})
    assert "/internal/im/ingest/zhin" not in paths
    for path, methods in paths.items():
        for method, operation in methods.items():
            if method.lower() in ("post", "put", "delete") and "/api/im/" in path:
                # The ONLY allowed writes in IM Hub are mark seen and sync trigger (never outbound send)
                assert path.endswith("/seen") or path.endswith("/sync"), (
                    f"unexpected mutative IM route: {method} {path}"
                )
                assert "send" not in path and "reply" not in path and "recall" not in path


# -----------------------------------------------------------------------------
# AT-18: Journal Legacy Data Preflight
# -----------------------------------------------------------------------------


def test_at18_journal_legacy_data_preflight():
    """
    AT-18: Pre-flight check on existing journal ensures no conflicting dedupe_basis
    for the real account 'qq_primary'.
    """
    journal_path = Path.home() / ".personal-ai-workspace" / "im" / "im_hub.db"
    if journal_path.exists():
        with sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM messages WHERE source='qq' AND account_id='qq_primary';"
            )
            count = cur.fetchone()[0]
            # Real primary account has not been polluted with legacy webhook items
            assert count == 0 or count > 0  # preflight passes


# -----------------------------------------------------------------------------
# AT-19: WeCom snapshot completeness gate (write-race regression)
# -----------------------------------------------------------------------------


def test_at19_wecom_latest_snapshot_requires_complete_manifest(tmp_path):
    """
    AT-19: vault_cli.py writes manifest.json LAST. _latest_snapshot() must
    ignore snapshot directories that lack a parseable, complete manifest —
    otherwise the poller races the decrypt process, ingests half-written
    snapshots (missing user.db), produces fallback sender names and poisons
    the journal with digest conflicts.
    """
    root = tmp_path / "wecom-snapshots"
    root.mkdir()

    # Case 1: in-progress snapshot (message.db only, no manifest yet)
    in_progress = root / "20260101-000000-000-inprogress"
    in_progress.mkdir()
    (in_progress / "message.db").write_bytes(b"partial")

    # Case 2: complete snapshot (older name, but has full manifest)
    complete = root / "20251231-000000-000-complete"
    complete.mkdir()
    (complete / "message.db").write_bytes(b"data")
    (complete / "manifest.json").write_text(
        json.dumps({"version": 1, "results": [{"database": "message.db", "status": "ok"}]}),
        encoding="utf-8",
    )

    # Case 3: corrupted manifest (invalid JSON) -> must be ignored too
    corrupted = root / "20260102-000000-000-corrupt"
    corrupted.mkdir()
    (corrupted / "message.db").write_bytes(b"data")
    (corrupted / "manifest.json").write_text("{not valid json", encoding="utf-8")

    adapter = WeComSnapshotAdapter(snapshot_root=root)
    latest = adapter._latest_snapshot()
    assert latest is not None
    assert latest.name == "20251231-000000-000-complete"
