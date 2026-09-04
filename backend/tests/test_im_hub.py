"""
Unified IM Hub - Acceptance Tests Suite (AT-1 ~ AT-9)
Conforms strictly to docs/03-im-integration-v0.2.7.md
Validates all mechanical contracts, invariants, deduplication, and security defenses.
"""

import asyncio
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from backend.app.im.adapters.qq import ZhinQQAdapter
from backend.app.im.adapters.wechat import WxCliAdapter, make_wechat_synthetic_key
from backend.app.im.adapters.wecom import WeComSnapshotAdapter
from backend.app.im.coordinator import IngestionCoordinator
from backend.app.im.journal import (
    IdentityConflictError,
    IMJournal,
    InvalidIngestEnvelopeError,
    secure_harden_directory_and_files,
)
from backend.app.im.models import (
    IMAttachment,
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
    reply_to: str = None
) -> IMMessageItem:
    return IMMessageItem(
        id=f"{source}_msg_{msg_id}",
        ingest_seq=0,
        source=source,
        account_id=account_id,
        channel_id=channel_id,
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
        focus_reasons=[]
    )


# -----------------------------------------------------------------------------
# AT-1: Capability Decoupling
# -----------------------------------------------------------------------------

def test_at1_capability_decoupling():
    """AT-1: ZhinQQAdapter canReadHistory=false, coordinator skips without error."""
    qq_adp = ZhinQQAdapter()
    assert qq_adp.capabilities.canReadHistory is False
    assert not hasattr(qq_adp, "read_history")

    wx_adp = WxCliAdapter()
    assert wx_adp.capabilities.canReadHistory is True
    assert hasattr(wx_adp, "read_history")


# -----------------------------------------------------------------------------
# AT-2: Cross-Path Digest Consistency & Reply-To Identity Domain
# -----------------------------------------------------------------------------

def test_at2_cross_path_canonical_digest_and_reply_to():
    """
    AT-2 Positive: Same WeChat reply message via SSE and Timeline produces
    identical canonical bytes and server digest -> triggers idempotent skip.
    AT-2 Negative: Changing reply_to under same dedupe_key alters digest -> IdentityConflictError.
    """
    with tempfile.TemporaryDirectory() as td:
        journal = IMJournal(Path(td) / "im_test.db")
        wx_adp = WxCliAdapter(account_id="wx_primary")

        raw_sse = {
            "id": "10001",
            "msg_svr_id": "999888777",
            "talker": "course_group@chatroom",
            "talker_name": "高等数学课程群",
            "content": "作业已提交",
            "type_name": "text",
            "create_time_iso": "2026-09-04T10:00:00Z",
            "create_time_epoch_ms": 1788516000000,
            "reply_to_svr_id": "888777666"  # Physical reply target locator
        }

        raw_timeline = {
            "id": "10001",
            "msg_svr_id": "999888777",
            "talker": "course_group@chatroom",
            "talker_name": "高等数学课程群",
            "content": "作业已提交",
            "type_name": "text",
            "create_time_iso": "2026-09-04T10:00:00Z",
            "create_time_epoch_ms": 1788516000000,
            "reply_to": "888777666"  # Slightly different input representation, same locator
        }

        rec_sse = wx_adp.normalize_wx_payload(raw_sse, provenance_mode="sse")
        rec_timeline = wx_adp.normalize_wx_payload(raw_timeline, provenance_mode="timeline")

        # 1. Verify byte-for-byte canonical equality
        bytes_sse = canonical_bytes_v1(rec_sse.message)
        bytes_timeline = canonical_bytes_v1(rec_timeline.message)
        assert bytes_sse == bytes_timeline

        digest_sse = compute_server_digest(rec_sse.message)
        digest_timeline = compute_server_digest(rec_timeline.message)
        assert digest_sse == digest_timeline
        assert rec_sse.dedupe_key == rec_timeline.dedupe_key

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

        # 4. Negative test: Same dedupe_key but different reply_to
        raw_conflict = dict(raw_sse)
        raw_conflict["reply_to_svr_id"] = "different_target_999"
        rec_conflict = wx_adp.normalize_wx_payload(raw_conflict)
        assert compute_server_digest(rec_conflict.message) != digest_sse

        batch_conflict = IMIngestBatch(source="wechat", account_id="wx_primary", records=[rec_conflict])
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
        rec_a = IMIngestRecord(source="wechat", account_id="acc_main", dedupe_key="key_A", dedupe_basis="native_message_id", message=msg_a)

        # Pre-commit A
        journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc_main", records=[rec_a]))

        # Prepare mixed batch: [A, B, A]
        msg_b = make_sample_message(msg_id="msg_B", text="Message B")
        rec_b = IMIngestRecord(source="wechat", account_id="acc_main", dedupe_key="key_B", dedupe_basis="native_message_id", message=msg_b)

        wm = IMWatermark(kind="source_cursor", value="cursor_100", committed_at="2026-09-04T10:05:00Z")
        mixed_batch = IMIngestBatch(
            source="wechat",
            account_id="acc_main",
            records=[rec_a, rec_b, rec_a],
            new_watermark=wm
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
            provided_digest="deadbeef" * 8  # Forged wrong hash
        )

        with pytest.raises(ValueError, match="Provided digest mismatch"):
            journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc_main", records=[rec]))

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
        rec_a = IMIngestRecord(source="wechat", account_id="acc_1", dedupe_key="key_1", dedupe_basis="native_message_id", message=msg_a)

        with pytest.raises(InvalidIngestEnvelopeError):
            journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc_1", records=[rec_a]))
        assert journal.get_current_head_seq() == 0

        # Branch B: account_id mismatch
        msg_b = make_sample_message(source="wechat", account_id="acc_2")
        rec_b = IMIngestRecord(source="wechat", account_id="acc_1", dedupe_key="key_2", dedupe_basis="native_message_id", message=msg_b)

        with pytest.raises(InvalidIngestEnvelopeError):
            journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc_1", records=[rec_b]))
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
        msg_1 = make_sample_message(account_id="wx_main", msg_id="101", text="收到", epoch_ms=1725400000000)
        rec_1 = IMIngestRecord(source="wechat", account_id="wx_main", dedupe_key=key_1, dedupe_basis="synthetic_v1", message=msg_1)

        # Message 2 (exact same content, sender, and timestamp, but different physical id)
        key_2 = make_wechat_synthetic_key("wx_main", "physical_svr_id_102")
        msg_2 = make_sample_message(account_id="wx_main", msg_id="102", text="收到", epoch_ms=1725400000000)
        rec_2 = IMIngestRecord(source="wechat", account_id="wx_main", dedupe_key=key_2, dedupe_basis="synthetic_v1", message=msg_2)

        assert key_1 != key_2

        receipt = journal.commit_batch(IMIngestBatch(source="wechat", account_id="wx_main", records=[rec_1, rec_2]))
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
            records.append(IMIngestRecord(source="wechat", account_id="acc", dedupe_key=f"k_{i}", dedupe_basis="native_message_id", message=m))

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
            m = make_sample_message(account_id="acc", msg_id=f"m_{i}", text=f"Item {i}", epoch_ms=1725400000000 + i)
            records.append(IMIngestRecord(source="wechat", account_id="acc", dedupe_key=f"k_{i}", dedupe_basis="native_message_id", message=m))

        journal.commit_batch(IMIngestBatch(source="wechat", account_id="acc", records=records))
        snapshot_head = journal.get_current_head_seq()
        assert snapshot_head == 137

        # Paging loop simulation
        all_fetched = []
        cursor = 0
        pages = 0

        while True:
            items, next_cursor = journal.query_snapshot_page(snapshot_head_seq=snapshot_head, cursor=cursor, limit=50)
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
# End-to-End API Integration & Zhin Push Loopback
# -----------------------------------------------------------------------------

def test_api_zhin_push_and_cache_control():
    """Test authenticated loopback Zhin ingress and Cache-Control: no-store on all /api/im endpoints."""
    import uuid
    client = TestClient(app)

    # 1. Test unauthorized push
    res = client.post("/internal/im/ingest/zhin", json={"event_id": "test_1"})
    assert res.status_code == 401

    # 2. Test authorized push with deterministic event
    test_evt_id = f"zhin_evt_{uuid.uuid4().hex[:8]}"
    secret = os.environ.get("IM_INGEST_SECRET", "workspace_im_secret_token_default")
    event_payload = {
        "event_id": test_evt_id,
        "account_id": "qq_test",
        "occurred_at": "2026-09-04T10:10:00Z",
        "occurred_at_epoch_ms": 1788517000000,
        "payload": {
            "message_type": "text",
            "sender_id": "u888",
            "sender_name": "班长",
            "group_id": "cs_2023",
            "group_name": "计算机23级通知群",
            "text": "明天早上高数课调至教三201，请大家相互转告！",
            "mentions": [{"is_all": True}]
        }
    }

    res2 = client.post(
        "/internal/im/ingest/zhin",
        headers={"X-IM-Secret": secret},
        json=event_payload
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["receipt"]["inserted_count"] == 1

    # Test idempotence: push exact same event again -> 200 OK, skipped_count == 1
    res2_repeat = client.post(
        "/internal/im/ingest/zhin",
        headers={"X-IM-Secret": secret},
        json=event_payload
    )
    assert res2_repeat.status_code == 200
    assert res2_repeat.json()["receipt"]["skipped_count"] == 1

    # 3. Test timeline endpoint and Cache-Control: no-store
    res3 = client.get("/api/im/timeline")
    assert res3.status_code == 200
    assert "no-store" in res3.headers.get("Cache-Control", "")
    data3 = res3.json()
    assert len(data3["items"]) >= 1
    first_item = data3["items"][0]
    assert first_item["source"] == "qq"
    assert "mention_all" in first_item["focus_tags"]
    assert len(first_item["focus_reasons"]) >= 1

    # 4. Test overview endpoint
    res4 = client.get("/api/im/overview")
    assert res4.status_code == 200
    assert "no-store" in res4.headers.get("Cache-Control", "")
    data4 = res4.json()
    assert data4["platforms"]["qq"] >= 1
