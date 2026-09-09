"""QQ local snapshot adapter.

Runtime boundary: this module reads only atomically published, standard SQLite
exports from qq-local-vault. It never imports LLDB/SQLCipher, starts a process,
controls QQ, accesses Tencent's container, performs network I/O, or sends IM.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.im.adapters.base import IMIngestDriver, IMIngestSink, IMSourceReader
from backend.app.im.adapters.qq_decode import decode_qq_message_blob
from backend.app.im.models import (
    IMAttachment,
    IMCapabilities,
    IMCoverage,
    IMCoverageGap,
    IMFreshness,
    IMIngestBatch,
    IMIngestRecord,
    IMMessageItem,
    IMSourceStatus,
    IMWatermark,
)
from backend.app.im.rules import evaluate_focus_rules

logger = logging.getLogger(__name__)

SNAPSHOT_RE = re.compile(r"^qqsnap-v1-[0-9a-f]{24}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
EXPECTED_SCHEMA = "qq.snapshot/v1"
EXPECTED_SCHEMA_PROFILE = "ntqq-macos-6.9.98-critical-schema-v1"
EXPECTED_SCHEMA_FINGERPRINT = "43b346101f88b2efdd6b2d16065c74c79d825fcbe6760bb43af7e0b5676eb81c"
EXPECTED_LOCATOR_PROFILE = "qq-locator-v1"
EXPECTED_NORMALIZATION_PROFILE = "qq-im-normalization-v1"
EXPECTED_EXPORTS = {
    "nt_msg": "export/nt_msg.db",
    "group_info": "export/group_info.db",
    "profile_info": "export/profile_info.db",
}

QQ_MESSAGE_TYPES = {
    2: "text",
    3: "file",
    5: "image",
    6: "voice",
    7: "video",
    8: "notice",
    9: "text",
    10: "link",
    11: "image",
    16: "link",
}


class QQSnapshotValidationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _epoch_iso(epoch: Optional[int]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _assert_private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise QQSnapshotValidationError("QQ_SNAPSHOT_PATH_REJECTED")
    stat_result = path.stat()
    if stat_result.st_uid != os.getuid() or (stat_result.st_mode & 0o077):
        raise QQSnapshotValidationError("QQ_SNAPSHOT_PERMISSION")


def _assert_private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise QQSnapshotValidationError("QQ_SNAPSHOT_PATH_REJECTED")
    stat_result = path.lstat()
    if stat_result.st_uid != os.getuid() or stat_result.st_nlink != 1 or (stat_result.st_mode & 0o077):
        raise QQSnapshotValidationError("QQ_SNAPSHOT_PERMISSION")


def make_qq_synthetic_key(account_id: str, table_role: str, physical_msg_id: int | str) -> str:
    """Stable, unambiguous source-side locator; never includes snapshot/content/time."""
    account = str(account_id)
    role = str(table_role)
    msg_id = str(physical_msg_id)
    return f"qq_locator:v1:{len(account)}:{account}:{len(role)}:{role}:i:{msg_id}"


def _opaque_message_id(account_id: str, locator: str) -> str:
    material = f"qq\x00{account_id}\x00{locator}".encode("utf-8")
    return "qq_msg_" + hashlib.sha256(material).hexdigest()[:32]


def _opaque_channel_id(account_id: str, channel_type: str, source_identity: str) -> str:
    material = f"{channel_type}\x00{source_identity}".encode("utf-8")
    return f"qq:{account_id}:{channel_type}:" + hashlib.sha256(material).hexdigest()[:24]


def _stable_sender_fallback(sender_id: Optional[str], *, notice: bool = False) -> str:
    if notice:
        return "QQ系统"
    if not sender_id:
        return "QQ用户"
    token = hashlib.sha256(sender_id.encode("utf-8")).hexdigest()[:8]
    return f"QQ用户 {token}"


class QQSnapshotAdapter(IMSourceReader, IMIngestDriver):
    """Read-only Reader/Driver over a validated append-only QQ snapshot vault."""

    def __init__(
        self,
        account_id: str = "qq_primary",
        snapshot_root: Optional[str | Path] = None,
        poll_interval_secs: float = 3.0,
        stale_after_secs: int = 30 * 60,
    ):
        if not ACCOUNT_RE.fullmatch(account_id):
            raise ValueError("invalid QQ account alias")
        self._account_id = account_id
        default_root = Path.home() / "Library/Application Support/qq-local-vault/accounts" / account_id
        self._snapshot_root = Path(snapshot_root).expanduser() if snapshot_root else default_root
        self._poll_interval = poll_interval_secs
        self._stale_after_secs = int(os.environ.get("QQ_SNAPSHOT_STALE_SECS", stale_after_secs))
        self._sink: Optional[IMIngestSink] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._connectivity = "offline"
        self._last_error_code: Optional[str] = None
        self._last_observed_at: Optional[str] = None
        self._committed_at: Optional[str] = None
        self._last_snapshot_id: Optional[str] = None
        self._last_good_manifest: Optional[Dict[str, Any]] = None
        self._last_good_paths: Optional[Dict[str, Path]] = None

    @property
    def source(self) -> str:
        return "qq"

    @property
    def capabilities(self) -> IMCapabilities:
        return IMCapabilities(
            canReadHistory=True,
            realtime=False,
            media="placeholder",
            nativeUnread=False,
            reliableSelfIdentity=False,
            mentions=False,
            replies=False,
            recallEvents=False,
        )

    def _reject_source_domain(self) -> None:
        path = self._snapshot_root.absolute()
        forbidden = Path.home() / "Library/Containers/com.tencent.qq"
        try:
            path.relative_to(forbidden)
        except ValueError:
            return
        raise QQSnapshotValidationError("QQ_SNAPSHOT_PATH_REJECTED")

    def _validate_current(self) -> tuple[Dict[str, Any], Dict[str, Path]]:
        self._reject_source_domain()
        root = self._snapshot_root
        if not root.exists():
            raise QQSnapshotValidationError("QQ_SNAPSHOT_MISSING")
        _assert_private_directory(root)
        _assert_private_directory(root / "snapshots")
        current = root / "CURRENT"
        _assert_private_file(current)
        try:
            snapshot_id = current.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise QQSnapshotValidationError("QQ_SNAPSHOT_PATH_REJECTED") from exc
        if not SNAPSHOT_RE.fullmatch(snapshot_id):
            raise QQSnapshotValidationError("QQ_SNAPSHOT_PATH_REJECTED")

        snapshot = root / "snapshots" / snapshot_id
        _assert_private_directory(snapshot)
        _assert_private_directory(snapshot / "export")
        manifest_path = snapshot / "manifest.json"
        _assert_private_file(manifest_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise QQSnapshotValidationError("QQ_SNAPSHOT_MANIFEST_INVALID") from exc

        required_values = {
            "schema": EXPECTED_SCHEMA,
            "snapshot_id": snapshot_id,
            "account_alias": self._account_id,
            "schema_profile_id": EXPECTED_SCHEMA_PROFILE,
            "critical_schema_fingerprint": EXPECTED_SCHEMA_FINGERPRINT,
            "locator_profile_id": EXPECTED_LOCATOR_PROFILE,
            "normalization_profile_id": EXPECTED_NORMALIZATION_PROFILE,
        }
        if any(manifest.get(key) != value for key, value in required_values.items()):
            raise QQSnapshotValidationError("QQ_SNAPSHOT_SCHEMA_UNSUPPORTED")
        core = dict(manifest)
        core.pop("snapshot_id", None)
        canonical = json.dumps(core, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected_id = "qqsnap-v1-" + hashlib.sha256(canonical).hexdigest()[:24]
        if expected_id != snapshot_id:
            raise QQSnapshotValidationError("QQ_SNAPSHOT_INTEGRITY_FAILED")

        listed = {item.get("role"): item for item in manifest.get("files", []) if isinstance(item, dict)}
        if set(listed) != set(EXPECTED_EXPORTS):
            raise QQSnapshotValidationError("QQ_SNAPSHOT_MANIFEST_INVALID")
        paths: Dict[str, Path] = {}
        for role, relative in EXPECTED_EXPORTS.items():
            item = listed[role]
            if item.get("path") != relative or not isinstance(item.get("sha256"), str):
                raise QQSnapshotValidationError("QQ_SNAPSHOT_PATH_REJECTED")
            path = snapshot / relative
            _assert_private_file(path)
            if path.stat().st_size != item.get("size") or _sha256(path) != item["sha256"]:
                raise QQSnapshotValidationError("QQ_SNAPSHOT_INTEGRITY_FAILED")
            with path.open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\x00":
                    raise QQSnapshotValidationError("QQ_SNAPSHOT_ENCRYPTED_SOURCE_REJECTED")
            paths[role] = path
        return manifest, paths

    async def get_status(self) -> IMSourceStatus:
        manifest = None
        error_code = None
        try:
            manifest, paths = await asyncio.to_thread(self._validate_current)
            self._last_good_manifest = manifest
            self._last_good_paths = paths
            self._last_observed_at = datetime.now(timezone.utc).isoformat()
        except QQSnapshotValidationError as exc:
            error_code = exc.code
            manifest = self._last_good_manifest

        if manifest is None:
            return IMSourceStatus(
                source="qq",
                connectivity="offline" if error_code == "QQ_SNAPSHOT_MISSING" else "error",
                coverage=IMCoverage(kind="unknown", gaps=[]),
                freshness=IMFreshness(stale=True, last_observed_at=self._last_observed_at),
                watermark=IMWatermark(kind="none", value=None, committed_at=None),
                rebuildability="none",
                detail=error_code,
            )

        coverage = manifest.get("coverage") or {}
        source_dt = _parse_iso(coverage.get("source_through_at"))
        lag_ms = max(0, int((datetime.now(timezone.utc) - source_dt).total_seconds() * 1000)) if source_dt else None
        stale = lag_ms is None or lag_ms > self._stale_after_secs * 1000
        from_iso = _epoch_iso(coverage.get("from_epoch"))
        through_iso = _epoch_iso(coverage.get("through_epoch"))
        gaps = [
            IMCoverageGap(from_time=from_iso or "", through_time=through_iso or "", reason=str(reason))
            for reason in coverage.get("gaps", [])
        ]
        connectivity = self._connectivity if self._running else "offline"
        if error_code or stale:
            connectivity = "degraded"
        return IMSourceStatus(
            source="qq",
            connectivity=connectivity,
            coverage=IMCoverage(kind="snapshot", from_time=from_iso, through_time=through_iso, gaps=gaps),
            freshness=IMFreshness(
                stale=stale,
                last_observed_at=self._last_observed_at,
                source_through_at=coverage.get("source_through_at"),
                lag_ms=lag_ms,
            ),
            watermark=IMWatermark(
                kind="snapshot_version",
                value=self._last_snapshot_id or (manifest.get("snapshot_id") if manifest else None),
                committed_at=self._committed_at,
            ),
            rebuildability="snapshot_bounded",
            detail=error_code,
        )

    async def start(self, sink: IMIngestSink) -> None:
        self._sink = sink
        self._running = True
        self._connectivity = "catching_up"
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        self._connectivity = "offline"
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._sink = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._ingest_current()
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except QQSnapshotValidationError as exc:
                self._last_error_code = exc.code
                self._connectivity = "degraded" if self._last_good_manifest else "error"
                logger.warning("QQ snapshot adapter degraded: %s", exc.code)
                await asyncio.sleep(min(60.0, self._poll_interval * 4))
            except Exception:
                self._last_error_code = "QQ_SNAPSHOT_INGEST_FAILED"
                self._connectivity = "degraded" if self._last_good_manifest else "error"
                logger.warning("QQ snapshot adapter degraded: QQ_SNAPSHOT_INGEST_FAILED")
                await asyncio.sleep(min(60.0, self._poll_interval * 4))

    async def _ingest_current(self) -> int:
        if self._sink is None:
            return 0
        manifest, paths = await asyncio.to_thread(self._validate_current)
        snapshot_id = manifest["snapshot_id"]
        self._last_good_manifest = manifest
        self._last_good_paths = paths
        self._last_observed_at = datetime.now(timezone.utc).isoformat()
        if snapshot_id == self._last_snapshot_id:
            self._connectivity = "live"
            return 0

        self._connectivity = "catching_up"
        records = await asyncio.to_thread(self._read_records, manifest, paths, False, None, None)
        committed_at = datetime.now(timezone.utc).isoformat()
        receipt = await self._sink.commit(
            IMIngestBatch(
                source="qq",
                account_id=self._account_id,
                records=records,
                new_watermark=IMWatermark(
                    kind="snapshot_version",
                    value=snapshot_id,
                    committed_at=committed_at,
                ),
            )
        )
        self._last_snapshot_id = snapshot_id
        self._committed_at = committed_at
        self._last_error_code = None
        self._connectivity = "live"
        return receipt.inserted_count

    def _open_ro(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _load_group_names(self, path: Path) -> Dict[str, str]:
        result: Dict[str, str] = {}
        with closing(self._open_ro(path)) as connection:
            query = '''
                SELECT g."60001" AS group_uin,
                       COALESCE(NULLIF(d."60026", ''), NULLIF(d."60007", ''), NULLIF(g."60007", '')) AS display_name
                FROM group_list g
                LEFT JOIN group_detail_info_ver1 d ON d."60001" = g."60001"
            '''
            for row in connection.execute(query):
                if row["group_uin"] is not None and row["display_name"]:
                    result[str(row["group_uin"])] = str(row["display_name"])
        return result

    def _load_buddy_names(self, path: Path) -> Dict[str, str]:
        result: Dict[str, str] = {}
        with closing(self._open_ro(path)) as connection:
            query = '''
                SELECT b."1000" AS uid, b."1002" AS uin,
                       COALESCE(NULLIF(p."20009", ''), NULLIF(p."20002", ''), NULLIF(b."1001", '')) AS display_name
                FROM buddy_list b
                LEFT JOIN profile_info_v6 p ON p."1000" = b."1000"
            '''
            for row in connection.execute(query):
                name = str(row["display_name"] or "").strip()
                if name:
                    if row["uid"]:
                        result[str(row["uid"])] = name
                    if row["uin"] is not None:
                        result[str(row["uin"])] = name
        return result

    def _read_records(
        self,
        manifest: Dict[str, Any],
        paths: Dict[str, Path],
        descending: bool,
        before_epoch: Optional[int],
        limit: Optional[int],
    ) -> List[IMIngestRecord]:
        group_names = self._load_group_names(paths["group_info"])
        buddy_names = self._load_buddy_names(paths["profile_info"])
        rows: list[tuple[str, sqlite3.Row]] = []
        order = "DESC" if descending else "ASC"
        with closing(self._open_ro(paths["nt_msg"])) as connection:
            for table_role, table in (("group", "group_msg_table"), ("c2c", "c2c_msg_table")):
                where = 'WHERE "40050" > 0'
                params: list[Any] = []
                if before_epoch is not None:
                    where += ' AND "40050" < ?'
                    params.append(before_epoch)
                query = f'''
                    SELECT "40001" AS msg_id, "40010" AS chat_type,
                           "40011" AS msg_type, "40012" AS sub_msg_type,
                           "40013" AS send_type, "40020" AS sender_uid,
                           "40021" AS peer_uid, "40030" AS peer_uin,
                           "40033" AS sender_uin, "40050" AS msg_time,
                           "40090" AS sender_member_name,
                           "40093" AS sender_nickname, "40800" AS body
                    FROM "{table}" {where}
                    ORDER BY "40050" {order}, "40001" {order}
                '''
                rows.extend((table_role, row) for row in connection.execute(query, params))
        rows.sort(key=lambda item: (int(item[1]["msg_time"] or 0), int(item[1]["msg_id"] or 0)), reverse=descending)
        if limit is not None:
            rows = rows[:limit]

        observed_at = datetime.now(timezone.utc).isoformat()
        records: List[IMIngestRecord] = []
        for table_role, row in rows:
            record = self._normalize_row(table_role, row, group_names, buddy_names, manifest["snapshot_id"], observed_at)
            if record is not None:
                records.append(record)
        return records

    def _normalize_row(
        self,
        table_role: str,
        row: sqlite3.Row,
        group_names: Dict[str, str],
        buddy_names: Dict[str, str],
        snapshot_id: str,
        observed_at: str,
    ) -> Optional[IMIngestRecord]:
        msg_id = row["msg_id"]
        timestamp = row["msg_time"]
        if msg_id is None or not timestamp or int(timestamp) <= 0:
            return None
        locator = make_qq_synthetic_key(self._account_id, table_role, msg_id)
        is_group = table_role == "group"
        if is_group:
            source_channel = str(row["peer_uin"] or row["peer_uid"] or "")
            if not source_channel:
                return None
            channel_name = group_names.get(source_channel) or "QQ群聊"
            channel_type = "group"
        else:
            source_channel = str(row["peer_uid"] or row["peer_uin"] or "")
            if not source_channel:
                return None
            channel_name = buddy_names.get(source_channel) or "QQ好友"
            channel_type = "direct"
        channel_id = _opaque_channel_id(self._account_id, channel_type, source_channel)

        sender_id = str(row["sender_uid"] or row["sender_uin"] or "") or None
        recorded_name = str(row["sender_member_name"] or row["sender_nickname"] or "").strip()
        numeric_type = int(row["msg_type"] or 0)
        message_type = QQ_MESSAGE_TYPES.get(numeric_type, "unknown")
        sender_name = recorded_name or _stable_sender_fallback(sender_id, notice=message_type == "notice")
        decoded = decode_qq_message_blob(row["body"])
        attachments = [
            IMAttachment(
                type=item["type"],
                name=item.get("name") or None,
                size=item.get("size") if isinstance(item.get("size"), int) else None,
                availability="placeholder",
                local_ref=None,
            )
            for item in decoded["attachments"]
            if item.get("type") in {"image", "voice", "video", "file"}
        ]
        if attachments and message_type == "text":
            message_type = "mixed"
        occurred_epoch = int(timestamp) * 1000
        tags, reasons = evaluate_focus_rules(
            channel_name=channel_name,
            channel_type=channel_type,
            is_focus=any(keyword in channel_name for keyword in ["通知", "班", "学院", "课程", "实验室", "科研", "竞赛"]),
            text=decoded["text"],
            message_type=message_type,
            mentions=[],
            source="qq",
            sender_name=sender_name,
            sender_id=sender_id,
            is_self=None,
        )
        message = IMMessageItem(
            id=_opaque_message_id(self._account_id, locator),
            ingest_seq=0,
            source="qq",
            account_id=self._account_id,
            channel_id=channel_id,
            channel_name=channel_name,
            source_id_quality="synthetic",
            source_message_id=locator,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_role=None,
            is_self=None,
            reply_to=None,
            text=decoded["text"],
            message_type=message_type,
            mentions=[],
            attachments=attachments,
            occurred_at=datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat(),
            occurred_at_epoch_ms=occurred_epoch,
            observed_at=observed_at,
            provenance={
                "mode": "snapshot",
                "snapshot_id": snapshot_id,
                "table_role": table_role,
                "locator_profile": EXPECTED_LOCATOR_PROFILE,
                "normalization_profile": EXPECTED_NORMALIZATION_PROFILE,
            },
            focus_tags=tags,
            focus_reasons=reasons,
        )
        return IMIngestRecord(
            source="qq",
            account_id=self._account_id,
            dedupe_key=locator,
            dedupe_basis="synthetic_v1",
            message=message,
        )

    async def read_history(self, limit: int = 50, before_cursor: Optional[str] = None) -> List[IMMessageItem]:
        manifest, paths = await asyncio.to_thread(self._validate_current)
        before_epoch = None
        if before_cursor:
            try:
                before_epoch = int(before_cursor)
            except ValueError:
                before_epoch = None
        records = await asyncio.to_thread(self._read_records, manifest, paths, True, before_epoch, limit)
        return [record.message for record in records]
