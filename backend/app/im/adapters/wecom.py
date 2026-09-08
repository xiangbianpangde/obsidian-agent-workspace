"""
Unified IM Hub - Enterprise WeChat (WeCom) Adapter (Real Snapshot Integration)
Conforms strictly to docs/03-im-integration-v0.2.7.md

Reads from a decrypted, read-only plaintext snapshot produced by
`yichen-wecom-local-vault` under:
    ~/Library/Application Support/wecom-local-vault/snapshots/<ts>-<dataset>/

Design:
- IMSourceReader: full snapshot history (bounded by snapshot availability)
- IMIngestDriver: snapshot-change detection (new snapshot directory appears)
- Dedupe identity: native `message_table.message_id` (physical record locator)
- is_self: null unless reliable self identity can be established (upstream caveat)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.app.im.adapters.base import IMIngestDriver, IMIngestSink, IMSourceReader
from backend.app.im.models import (
    IMAttachment,
    IMCapabilities,
    IMCoverage,
    IMFreshness,
    IMIngestBatch,
    IMIngestRecord,
    IMMessageItem,
    IMSourceStatus,
    IMWatermark,
)
from backend.app.im.rules import evaluate_focus_rules

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_ROOT = Path.home() / "Library/Application Support/wecom-local-vault/snapshots"

# WeCom content_type -> normalized message type
WECOM_TYPE_MAP = {
    0: "text",
    3: "image",
    4: "file",
    13: "link",
    14: "file",
    15: "file",
    16: "file",
    17: "file",
    18: "file",
    22: "voice",
    24: "file",
    29: "image",
    34: "voice",
    43: "video",
    1001: "text",
    1011: "notice",   # meeting notice
    2001: "text",
    3001: "link",
    4001: "notice",
}


def make_wecom_synthetic_key(account_id: str, physical_msg_id: str) -> str:
    """
    Synthetic key invariant (P1-IM-6-R1 & AT-6):
    MUST contain a stable source-side physical record locator.
    """
    return f"wecom_locator:{account_id}:{physical_msg_id}"


def _clean_text(value: str) -> str:
    value = "".join(ch if ch in "\n\t" or ch.isprintable() else " " for ch in value)
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _read_varint(data: bytes, position: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while position < len(data):
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        if shift > 63:
            break
    raise ValueError("invalid varint")


def _protobuf_text(data: bytes, depth: int = 0) -> List[str]:
    """Recursively extract readable UTF-8 strings from a protobuf blob."""
    if depth > 4 or not data:
        return []
    position = 0
    values: List[str] = []
    try:
        while position < len(data):
            tag, position = _read_varint(data, position)
            wire_type = tag & 7
            if tag == 0:
                return []
            if wire_type == 0:
                _, position = _read_varint(data, position)
            elif wire_type == 1:
                position += 8
            elif wire_type == 5:
                position += 4
            elif wire_type == 2:
                length, position = _read_varint(data, position)
                if position + length > len(data):
                    return []
                segment = data[position : position + length]
                position += length
                try:
                    text = _clean_text(segment.decode("utf-8")) if b"\x00" not in segment else ""
                except UnicodeDecodeError:
                    text = ""
                if len(text) >= 2 and not re.fullmatch(r"[0-9a-fA-F]{32,}", text):
                    values.append(text)
                else:
                    values.extend(_protobuf_text(segment, depth + 1))
            else:
                return []
            if position > len(data):
                return []
    except (ValueError, IndexError):
        return []

    deduped: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def decode_wecom_content(raw: Any) -> str:
    """Decode WeCom message content (plain UTF-8 or protobuf-encoded blob)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return _clean_text(raw)
    data = bytes(raw)
    if not data:
        return ""
    try:
        plain = data.decode("utf-8")
        controls = sum(1 for byte in data if byte < 32 and byte not in (9, 10, 13))
        if controls / len(data) <= 0.08:
            return _clean_text(plain)
    except UnicodeDecodeError:
        pass
    values = _protobuf_text(data)
    if values:
        return "\n".join(values[:12])
    return f"[二进制内容 {len(data)} 字节]"


class WeComSnapshotAdapter(IMSourceReader, IMIngestDriver):
    """
    Real WeCom adapter reading decrypted plaintext snapshots.
    canReadHistory=True, coverage=snapshot, rebuildability=snapshot_bounded.
    """

    def __init__(
        self,
        account_id: str = "wecom_primary",
        snapshot_root: Optional[str | Path] = None,
        poll_interval_secs: float = 15.0,
        backfill_days: int = 14,
    ):
        self._account_id = account_id
        self._snapshot_root = Path(snapshot_root) if snapshot_root else DEFAULT_SNAPSHOT_ROOT
        self._poll_interval = poll_interval_secs
        self._backfill_days = backfill_days

        self._sink: Optional[IMIngestSink] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        self._active_snapshot: Optional[Path] = None
        self._ingested_snapshots: set = set()
        self._last_observed_at: Optional[str] = None
        self._last_watermark_val: Optional[str] = None
        self._connectivity = "offline"
        self._last_error: Optional[str] = None
        self._self_user_ids: set = set()

    # -------------------------------------------------------------------------
    # Capability & Status
    # -------------------------------------------------------------------------

    @property
    def source(self) -> str:
        return "wecom"

    @property
    def capabilities(self) -> IMCapabilities:
        return IMCapabilities(
            canReadHistory=True,
            realtime=False,
            media="placeholder",
            nativeUnread=True,
            reliableSelfIdentity=True,   # self user id is resolvable from user_table/company.db
            mentions=True,
            replies=False,
            recallEvents=False,
        )

    async def get_status(self) -> IMSourceStatus:
        snap = self._latest_snapshot()
        snapshot_at = None
        stale = True
        lag_ms = None

        if snap is not None:
            snapshot_at = datetime.fromtimestamp(snap.stat().st_mtime, tz=timezone.utc).isoformat()
            lag_ms = max(0, int((datetime.now(timezone.utc).timestamp() - snap.stat().st_mtime) * 1000))
            stale = lag_ms > 30 * 60 * 1000

        return IMSourceStatus(
            source="wecom",
            connectivity=self._connectivity if self._running else "offline",
            coverage=IMCoverage(kind="snapshot", gaps=[]),
            freshness=IMFreshness(
                stale=stale,
                last_observed_at=self._last_observed_at or snapshot_at,
                source_through_at=snapshot_at,
                lag_ms=lag_ms,
            ),
            watermark=IMWatermark(
                kind="snapshot_version",
                value=self._last_watermark_val or (snap.name if snap else None),
                committed_at=self._last_observed_at,
            ),
            rebuildability="snapshot_bounded",
        )

    # -------------------------------------------------------------------------
    # Snapshot discovery
    # -------------------------------------------------------------------------

    def _latest_snapshot(self) -> Optional[Path]:
        if not self._snapshot_root.is_dir():
            return None
        candidates = [
            p for p in self._snapshot_root.iterdir()
            if p.is_dir() and (p / "message.db").exists()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.name)

    # -------------------------------------------------------------------------
    # IMIngestDriver
    # -------------------------------------------------------------------------

    async def start(self, sink: IMIngestSink) -> None:
        self._sink = sink
        self._running = True
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
        # Initial backfill
        await self._ingest_snapshot(days_back=self._backfill_days)

        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if not self._running:
                    break
                await self._ingest_snapshot(days_back=0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_error = str(e)
                self._connectivity = "degraded"
                logger.warning("WeCom poll error: %s", e)
                await asyncio.sleep(min(60.0, self._poll_interval * 4))

    async def _ingest_snapshot(self, days_back: int = 0) -> int:
        snap = self._latest_snapshot()
        if snap is None or self._sink is None:
            self._connectivity = "offline"
            return 0

        self._active_snapshot = snap
        snap_key = snap.name

        # First pass on this snapshot: full backfill. Later passes: incremental by cursor.
        is_new_snapshot = snap_key not in self._ingested_snapshots

        records = await asyncio.to_thread(
            self._read_snapshot_records,
            snap,
            self._last_watermark_val if not is_new_snapshot else None,
            days_back if is_new_snapshot else 0,
        )

        self._ingested_snapshots.add(snap_key)

        if not records:
            self._connectivity = "live"
            self._last_watermark_val = snap_key
            self._last_observed_at = datetime.now(timezone.utc).isoformat()
            return 0

        # Watermark: snapshot version (bounded rebuildability)
        new_wm = IMWatermark(
            kind="snapshot_version",
            value=snap_key,
            committed_at=datetime.now(timezone.utc).isoformat(),
        )

        batch = IMIngestBatch(
            source="wecom",
            account_id=self._account_id,
            records=records,
            new_watermark=new_wm,
        )
        receipt = await self._sink.commit(batch)

        self._last_watermark_val = snap_key
        self._last_observed_at = datetime.now(timezone.utc).isoformat()
        self._connectivity = "live"
        return receipt.inserted_count

    # -------------------------------------------------------------------------
    # Snapshot reading (runs in a worker thread; strictly read-only)
    # -------------------------------------------------------------------------

    def _read_snapshot_records(
        self,
        snap: Path,
        cursor: Optional[str],
        days_back: int,
    ) -> List[IMIngestRecord]:
        msg_db = snap / "message.db"
        sess_db = snap / "session.db"
        user_db = snap / "user.db"
        if not msg_db.exists():
            return []

        # Load channel names
        channel_names: Dict[str, str] = {}
        if sess_db.exists():
            try:
                sc = sqlite3.connect(f"file:{sess_db}?mode=ro", uri=True)
                for cid, name in sc.execute("SELECT id, name FROM conversation_table;"):
                    if cid:
                        channel_names[str(cid)] = (name or str(cid))
                sc.close()
            except Exception as e:
                logger.warning("WeCom session.db read failed: %s", e)

        # Load user names + resolve self identity
        user_names: Dict[int, str] = {}
        if user_db.exists():
            try:
                uc = sqlite3.connect(f"file:{user_db}?mode=ro", uri=True)
                for uid, name in uc.execute("SELECT id, name FROM user_table;"):
                    if uid is not None:
                        user_names[int(uid)] = (name or str(uid))
                uc.close()
            except Exception as e:
                logger.warning("WeCom user.db read failed: %s", e)

        self._resolve_self_identity(snap)

        now_ts = int(datetime.now(timezone.utc).timestamp())
        since_ts = 0
        if days_back > 0:
            since_ts = now_ts - days_back * 86400
        elif cursor:
            try:
                since_ts = int(cursor)
            except ValueError:
                since_ts = 0

        records: List[IMIngestRecord] = []
        try:
            mc = sqlite3.connect(f"file:{msg_db}?mode=ro", uri=True)
            mc.row_factory = sqlite3.Row
            query = """
                SELECT message_id, server_id, sequence, sender_id, conversation_id,
                       content_type, send_time, content
                FROM message_table
                WHERE send_time >= ?
                ORDER BY send_time ASC
                LIMIT 20000;
            """
            for row in mc.execute(query, (since_ts,)):
                rec = self._normalize_row(dict(row), channel_names, user_names)
                if rec is not None:
                    records.append(rec)
            mc.close()
        except Exception as e:
            logger.warning("WeCom message.db read failed: %s", e)
            raise

        return records

    def _resolve_self_identity(self, snap: Path) -> None:
        """
        Best-effort self user id discovery.
        The account data directory name is the WeCom user id (e.g. 1688857608826794),
        which is also present in user.db:user_table.id.
        """
        if self._self_user_ids:
            return
        try:
            self._self_user_ids.add(int(self._account_id))
        except (TypeError, ValueError):
            pass

        user_db = snap / "user.db"
        if not user_db.exists():
            return
        try:
            c = sqlite3.connect(f"file:{user_db}?mode=ro", uri=True)
            cur = c.cursor()
            if self._self_user_ids:
                for uid in list(self._self_user_ids):
                    cur.execute("SELECT 1 FROM user_table WHERE id = ? LIMIT 1;", (uid,))
                    if cur.fetchone() is None:
                        self._self_user_ids.discard(uid)
            c.close()
        except Exception:
            pass

    def _normalize_row(
        self,
        row: Dict[str, Any],
        channel_names: Dict[str, str],
        user_names: Dict[int, str],
    ) -> Optional[IMIngestRecord]:
        msg_id = row.get("message_id")
        if msg_id in (None, 0, "0"):
            return None

        physical_id = str(msg_id)
        dedupe_key = make_wecom_synthetic_key(self._account_id, physical_id)

        conv_id = str(row.get("conversation_id") or "unknown")
        channel_id = f"wecom:{conv_id}"
        channel_name = channel_names.get(conv_id) or self._fallback_channel_name(conv_id)
        channel_type = "group" if (conv_id.startswith("R:") or conv_id.startswith("S:")) else "direct"

        send_time = int(row.get("send_time") or 0)
        occurred_iso = datetime.fromtimestamp(send_time, tz=timezone.utc).isoformat()

        content_type = int(row.get("content_type") or 0)
        message_type = WECOM_TYPE_MAP.get(content_type, "unknown")

        text = self._extract_content(row.get("content"), content_type)

        sender_id_int = row.get("sender_id")
        sender_id = str(sender_id_int) if sender_id_int is not None else None
        sender_name = user_names.get(int(sender_id_int)) if sender_id_int is not None else None
        sender_name = sender_name or f"企微用户 {sender_id}"

        is_self: Optional[bool] = None
        if sender_id_int is not None and self._self_user_ids:
            is_self = int(sender_id_int) in self._self_user_ids

        mentions = self._extract_mentions(text)

        tags, reasons = evaluate_focus_rules(
            channel_name=channel_name,
            channel_type=channel_type,
            is_focus=self._is_focus_channel(channel_name),
            text=text,
            message_type=message_type,
            mentions=mentions,
            source="wecom",
            sender_name=sender_name,
            sender_id=sender_id,
            is_self=is_self,
        )

        attachments: List[IMAttachment] = []
        if message_type in ("image", "voice", "video", "file"):
            attachments.append(IMAttachment(type=message_type, availability="placeholder"))

        msg_item = IMMessageItem(
            id=f"wecom_msg_{physical_id}",
            ingest_seq=0,
            source="wecom",
            account_id=self._account_id,
            channel_id=channel_id,
            channel_name=channel_name,
            source_id_quality="native",
            source_message_id=physical_id,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_role=None,
            is_self=is_self,
            reply_to=None,
            text=text,
            message_type=message_type,
            mentions=mentions,
            attachments=attachments,
            occurred_at=occurred_iso,
            occurred_at_epoch_ms=send_time * 1000,
            observed_at=datetime.now(timezone.utc).isoformat(),
            provenance={"mode": "snapshot", "snapshot_id": self._active_snapshot.name if self._active_snapshot else None},
            focus_tags=tags,
            focus_reasons=reasons,
        )

        return IMIngestRecord(
            source="wecom",
            account_id=self._account_id,
            dedupe_key=dedupe_key,
            dedupe_basis="synthetic_v1",
            message=msg_item,
        )

    @staticmethod
    def _extract_content(raw: Any, content_type: int) -> str:
        """Decode WeCom payload (handles protobuf-encoded blobs)."""
        return decode_wecom_content(raw)

    @staticmethod
    def _fallback_channel_name(conv_id: str) -> str:
        if conv_id == "ANNOUNCE":
            return "企微公告"
        return f"企微会话 {conv_id}"

    @staticmethod
    def _is_focus_channel(channel_name: str) -> bool:
        return any(
            kw in channel_name
            for kw in ["通知", "班", "学院", "教务", "课程", "科研", "实验室", "导师", "辅导", "公告", "大学", "数学", "物理", "统计", "英语"]
        )

    @staticmethod
    def _extract_mentions(text: str) -> List[Dict[str, Any]]:
        mentions: List[Dict[str, Any]] = []
        if "@所有人" in text or "@全体成员" in text or "@all" in text.lower():
            mentions.append({"is_all": True})
        return mentions

    # -------------------------------------------------------------------------
    # IMSourceReader
    # -------------------------------------------------------------------------

    async def read_history(self, limit: int = 50, before_cursor: Optional[str] = None) -> List[IMMessageItem]:
        snap = self._latest_snapshot()
        if snap is None:
            return []
        records = await asyncio.to_thread(self._read_snapshot_records, snap, before_cursor, self._backfill_days)
        return [r.message for r in records[:limit]]
