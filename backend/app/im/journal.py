"""
Unified IM Hub - Local Derived IM Journal (SQLite)
Conforms strictly to docs/03-im-integration-v0.2.7.md
Implements transactional deduplication, permission hardening (0700/0600),
and deterministic Keyset pagination.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.app.im.models import (
    IMAttachment,
    IMChannelSummary,
    IMCommitReceipt,
    IMIngestBatch,
    IMIngestRecord,
    IMMessageItem,
    IMWatermark,
    compute_server_digest,
)


class IdentityConflictError(Exception):
    """Raised when a dedupe_key collides with a different objective payload digest."""
    pass


class InvalidIngestEnvelopeError(ValueError):
    """Raised when envelope source/account_id mismatches inner message source/account_id."""
    pass


def secure_harden_directory_and_files(db_path: Path) -> None:
    """
    Security invariant (P1-IM-4 & AT-9):
    1. Active umask(0077)
    2. Chmod directory to 0700
    3. Chmod existing db, -wal, -shm to 0600
    4. Assert no group or world read/write/exec bits remain. Fail closed if not secured.
    """
    os.umask(0o077)
    db_dir = db_path.parent
    db_dir.mkdir(parents=True, exist_ok=True)
    
    # Secure directory
    os.chmod(db_dir, 0o700)
    dir_stat = os.stat(db_dir)
    if dir_stat.st_mode & 0o077 != 0:
        raise PermissionError(f"Security invariant violated: directory {db_dir} has broad permissions {oct(dir_stat.st_mode)}")

    # Secure database file and WAL/SHM if they exist
    for target in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
        if target.exists():
            os.chmod(target, 0o600)
            f_stat = os.stat(target)
            if f_stat.st_mode & 0o077 != 0:
                raise PermissionError(f"Security invariant violated: file {target} has broad permissions {oct(f_stat.st_mode)}")


class IMJournal:
    """
    SQLite-backed local derived IM Journal.
    authority = derived, rebuildable per source.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        if db_path is None:
            home = Path.home()
            default_dir = home / ".personal-ai-workspace" / "im"
            self.db_path = default_dir / "im_hub.db"
        else:
            self.db_path = Path(db_path).expanduser().resolve()

        # Execute permission hardening before opening DB
        secure_harden_directory_and_files(self.db_path)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None  # Explicit transaction management
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            # Enable WAL mode for high concurrency read/write
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")

            # Channels summary table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                account_id TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                name TEXT NOT NULL,
                avatar_availability TEXT NOT NULL DEFAULT 'placeholder',
                avatar_local_ref TEXT,
                last_message TEXT NOT NULL DEFAULT '',
                last_time TEXT NOT NULL DEFAULT '',
                last_occurred_at_epoch_ms INTEGER NOT NULL DEFAULT 0,
                local_unseen_count INTEGER NOT NULL DEFAULT 0,
                is_focus INTEGER NOT NULL DEFAULT 0,
                native_unread_count INTEGER
            );
            """)

            # Messages table with physical UNIQUE constraint
            cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL,
                account_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                dedupe_basis TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                source_message_id TEXT,
                source_id_quality TEXT NOT NULL,
                sender_id TEXT,
                sender_name TEXT NOT NULL,
                sender_role TEXT,
                is_self INTEGER,
                reply_to TEXT,
                text TEXT NOT NULL,
                message_type TEXT NOT NULL,
                mentions_json TEXT NOT NULL,
                attachments_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                occurred_at_epoch_ms INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                focus_tags_json TEXT NOT NULL,
                focus_reasons_json TEXT NOT NULL,
                UNIQUE (source, account_id, dedupe_key)
            );
            """)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_timeline ON messages (occurred_at_epoch_ms DESC, ingest_seq DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages (channel_id, ingest_seq ASC);")

            # Watermarks table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS watermarks (
                source TEXT NOT NULL,
                account_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT,
                committed_at TEXT NOT NULL,
                PRIMARY KEY (source, account_id)
            );
            """)
            cur.close()

            # Ensure newly created WAL/SHM are also hardened
            secure_harden_directory_and_files(self.db_path)

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()

    def get_current_head_seq(self) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COALESCE(MAX(ingest_seq), 0) AS head FROM messages;")
            row = cur.fetchone()
            cur.close()
            return int(row["head"]) if row else 0

    # -------------------------------------------------------------------------
    # Ingestion Transaction (P1-IM-6 & AT-3, AT-4, AT-5)
    # -------------------------------------------------------------------------

    def commit_batch(self, batch: IMIngestBatch) -> IMCommitReceipt:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")

                inserted_count = 0
                skipped_count = 0

                for rec in batch.records:
                    # 0. Envelope-message consistency boundary (P1-IM-6-R3 & AT-5A, AT-5B)
                    if rec.source != rec.message.source or rec.account_id != rec.message.account_id:
                        raise InvalidIngestEnvelopeError(
                            f"InvalidIngestEnvelope: source/account_id mismatch between envelope ({rec.source}/{rec.account_id}) and message ({rec.message.source}/{rec.message.account_id})"
                        )

                    # 1. Server computes authoritative digest (P1-IM-6 & AT-2)
                    server_digest = compute_server_digest(rec.message)
                    if rec.provided_digest and rec.provided_digest != server_digest:
                        raise ValueError(f"Provided digest mismatch: provided '{rec.provided_digest}' != computed '{server_digest}'")

                    # 2. Check existing record by (source, account_id, dedupe_key)
                    cur.execute(
                        "SELECT payload_digest FROM messages WHERE source = ? AND account_id = ? AND dedupe_key = ?;",
                        (rec.source, rec.account_id, rec.dedupe_key)
                    )
                    row = cur.fetchone()

                    if row is not None:
                        existing_digest = row["payload_digest"]
                        if existing_digest == server_digest:
                            # Idempotent skip for this record; continue processing remaining batch items
                            skipped_count += 1
                            continue
                        else:
                            # Severe Identity Conflict! Must rollback whole batch and not advance watermark
                            raise IdentityConflictError(
                                f"IdentityConflictError for {rec.source}/{rec.account_id}/{rec.dedupe_key}: existing digest {existing_digest} != new digest {server_digest}"
                            )

                    # 3. New record insertion
                    # Serialize complex fields to JSON
                    mentions_str = json.dumps(rec.message.mentions, ensure_ascii=False)
                    att_dicts = [asdict(a) if isinstance(a, IMAttachment) else a for a in rec.message.attachments]
                    attachments_str = json.dumps(att_dicts, ensure_ascii=False)
                    provenance_str = json.dumps(rec.message.provenance, ensure_ascii=False)
                    focus_tags_str = json.dumps(rec.message.focus_tags, ensure_ascii=False)
                    focus_reasons_str = json.dumps(rec.message.focus_reasons, ensure_ascii=False)

                    is_self_int = 1 if rec.message.is_self is True else (0 if rec.message.is_self is False else None)

                    cur.execute("""
                    INSERT INTO messages (
                        id, source, account_id, channel_id, dedupe_key, dedupe_basis,
                        payload_digest, source_message_id, source_id_quality, sender_id,
                        sender_name, sender_role, is_self, reply_to, text, message_type,
                        mentions_json, attachments_json, occurred_at, occurred_at_epoch_ms,
                        observed_at, provenance_json, focus_tags_json, focus_reasons_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?
                    );
                    """, (
                        rec.message.id, rec.source, rec.account_id, rec.message.channel_id, rec.dedupe_key, rec.dedupe_basis,
                        server_digest, rec.message.source_message_id, rec.message.source_id_quality, rec.message.sender_id,
                        rec.message.sender_name, rec.message.sender_role, is_self_int, rec.message.reply_to, rec.message.text, rec.message.message_type,
                        mentions_str, attachments_str, rec.message.occurred_at, rec.message.occurred_at_epoch_ms,
                        rec.message.observed_at, provenance_str, focus_tags_str, focus_reasons_str
                    ))
                    inserted_count += 1

                    # Update or register channel summary
                    self._upsert_channel_for_message(cur, rec.message)

                # 4. Atomic Watermark advance within the same transaction (Core Invariant)
                watermark_advanced = False
                if batch.new_watermark is not None:
                    committed_at = batch.new_watermark.committed_at or datetime.now(timezone.utc).isoformat()
                    cur.execute("""
                    INSERT INTO watermarks (source, account_id, kind, value, committed_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source, account_id) DO UPDATE SET
                        kind = excluded.kind,
                        value = excluded.value,
                        committed_at = excluded.committed_at;
                    """, (
                        batch.source, batch.account_id, batch.new_watermark.kind,
                        batch.new_watermark.value, committed_at
                    ))
                    watermark_advanced = True

                cur.execute("COMMIT;")

                # Retrieve current head sequence
                cur.execute("SELECT COALESCE(MAX(ingest_seq), 0) AS head FROM messages;")
                head = int(cur.fetchone()["head"])

                return IMCommitReceipt(
                    source=batch.source,
                    account_id=batch.account_id,
                    inserted_count=inserted_count,
                    skipped_count=skipped_count,
                    committed_seq_head=head,
                    watermark_advanced=watermark_advanced
                )

            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def _upsert_channel_for_message(self, cur: sqlite3.Cursor, msg: IMMessageItem) -> None:
        """Update channel last_message, last_time, unseen count."""
        is_self = msg.is_self is True
        cur.execute("SELECT id, local_unseen_count FROM channels WHERE id = ?;", (msg.channel_id,))
        row = cur.fetchone()

        if row is None:
            # Create channel entry
            name = msg.channel_id.split(":", 1)[-1]
            unseen = 0 if is_self else 1
            cur.execute("""
            INSERT INTO channels (
                id, platform, account_id, channel_type, name,
                avatar_availability, last_message, last_time,
                last_occurred_at_epoch_ms, local_unseen_count, is_focus
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                msg.channel_id, msg.source, msg.account_id, "direct" if "direct" in msg.channel_id else "group",
                name, "placeholder", msg.text[:100], msg.occurred_at, msg.occurred_at_epoch_ms,
                unseen, 1 if ("通知" in name or "班" in name) else 0
            ))
        else:
            unseen = row["local_unseen_count"] + (0 if is_self else 1)
            cur.execute("""
            UPDATE channels SET
                last_message = ?,
                last_time = ?,
                last_occurred_at_epoch_ms = MAX(last_occurred_at_epoch_ms, ?),
                local_unseen_count = ?
            WHERE id = ?;
            """, (msg.text[:100], msg.occurred_at, msg.occurred_at_epoch_ms, unseen, msg.channel_id))

    # -------------------------------------------------------------------------
    # Channel & Timeline Query APIs
    # -------------------------------------------------------------------------

    def list_channels(self, platform: Optional[str] = None, channel_type: Optional[str] = None, focus_only: bool = False) -> List[IMChannelSummary]:
        with self._lock:
            cur = self._conn.cursor()
            query = "SELECT * FROM channels WHERE 1=1"
            params: List[Any] = []

            if platform:
                query += " AND platform = ?"
                params.append(platform)
            if channel_type:
                query += " AND channel_type = ?"
                params.append(channel_type)
            if focus_only:
                query += " AND is_focus = 1"

            query += " ORDER BY last_occurred_at_epoch_ms DESC;"
            cur.execute(query, params)
            rows = cur.fetchall()
            cur.close()

            results: List[IMChannelSummary] = []
            for r in rows:
                results.append(IMChannelSummary(
                    id=r["id"],
                    platform=r["platform"],
                    account_id=r["account_id"],
                    channel_type=r["channel_type"],
                    name=r["name"],
                    avatar_availability=r["avatar_availability"],
                    avatar_local_ref=r["avatar_local_ref"],
                    last_message=r["last_message"],
                    last_time=r["last_time"],
                    local_unseen_count=r["local_unseen_count"],
                    is_focus=bool(r["is_focus"]),
                    native_unread_count=r["native_unread_count"]
                ))
            return results

    def mark_channel_seen(self, channel_id: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("UPDATE channels SET local_unseen_count = 0 WHERE id = ?;", (channel_id,))
            cur.close()

    def query_timeline(
        self,
        platform: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
        snapshot_seq: Optional[int] = None,
        focus_only: bool = False
    ) -> Tuple[List[IMMessageItem], Optional[str]]:
        """
        Keyset pagination ordered by (occurred_at_epoch_ms DESC, ingest_seq DESC).
        Cursor is base64 encoded JSON: {"epoch_ms": ..., "ingest_seq": ...}
        If snapshot_seq is provided, strictly enforces: WHERE ingest_seq <= snapshot_seq
        """
        with self._lock:
            cur = self._conn.cursor()
            query = "SELECT * FROM messages WHERE 1=1"
            params: List[Any] = []

            if platform:
                query += " AND source = ?"
                params.append(platform)
            if snapshot_seq is not None:
                query += " AND ingest_seq <= ?"
                params.append(snapshot_seq)
            if focus_only:
                query += " AND (focus_tags_json != '[]' AND focus_tags_json IS NOT NULL)"

            if cursor:
                try:
                    c_data = json.loads(base64.b64decode(cursor.encode("utf-8")).decode("utf-8"))
                    c_epoch = c_data["epoch_ms"]
                    c_seq = c_data["ingest_seq"]
                    query += " AND (occurred_at_epoch_ms < ? OR (occurred_at_epoch_ms = ? AND ingest_seq < ?))"
                    params.extend([c_epoch, c_epoch, c_seq])
                except Exception:
                    pass

            query += " ORDER BY occurred_at_epoch_ms DESC, ingest_seq DESC LIMIT ?;"
            params.append(limit + 1)  # Fetch one extra to determine next_cursor

            cur.execute(query, params)
            rows = cur.fetchall()
            cur.close()

            has_more = len(rows) > limit
            target_rows = rows[:limit] if has_more else rows

            items = [self._row_to_message(r) for r in target_rows]
            next_cursor = None
            if has_more and items:
                last_item = items[-1]
                cursor_payload = {
                    "epoch_ms": last_item.occurred_at_epoch_ms,
                    "ingest_seq": last_item.ingest_seq
                }
                next_cursor = base64.b64encode(json.dumps(cursor_payload).encode("utf-8")).decode("utf-8")

            return items, next_cursor

    def query_snapshot_page(self, snapshot_head_seq: int, cursor: int, limit: int = 50) -> Tuple[List[IMMessageItem], Optional[int]]:
        """
        Deterministic pagination for Resync Snapshot Exhaustion (P1-IM-7-R1 & AT-8):
        WHERE ingest_seq > :cursor AND ingest_seq <= :snapshot_head_seq ORDER BY ingest_seq ASC LIMIT :limit
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
            SELECT * FROM messages
            WHERE ingest_seq > ? AND ingest_seq <= ?
            ORDER BY ingest_seq ASC
            LIMIT ?;
            """, (cursor, snapshot_head_seq, limit))
            rows = cur.fetchall()
            cur.close()

            items = [self._row_to_message(r) for r in rows]
            if not items:
                return [], None

            last_seq = items[-1].ingest_seq
            # Check if additional records <= snapshot_head_seq remain
            cur = self._conn.cursor()
            cur.execute("SELECT 1 FROM messages WHERE ingest_seq > ? AND ingest_seq <= ? LIMIT 1;", (last_seq, snapshot_head_seq))
            has_next = cur.fetchone() is not None
            cur.close()

            next_cursor = last_seq if has_next else None
            return items, next_cursor

    def query_replay_events(self, after_seq: int, limit: int = 200) -> List[IMMessageItem]:
        """Replays events where ingest_seq > after_seq (Open interval, P2-1 & AT-7)."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
            SELECT * FROM messages
            WHERE ingest_seq > ?
            ORDER BY ingest_seq ASC
            LIMIT ?;
            """, (after_seq, limit))
            rows = cur.fetchall()
            cur.close()
            return [self._row_to_message(r) for r in rows]

    def get_source_watermark(self, source: str, account_id: str) -> Optional[IMWatermark]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM watermarks WHERE source = ? AND account_id = ?;", (source, account_id))
            row = cur.fetchone()
            cur.close()
            if row is None:
                return None
            return IMWatermark(
                kind=row["kind"],
                value=row["value"],
                committed_at=row["committed_at"]
            )

    def _row_to_message(self, r: sqlite3.Row) -> IMMessageItem:
        raw_mentions = json.loads(r["mentions_json"])
        raw_attachments = json.loads(r["attachments_json"])
        attachments = [
            IMAttachment(
                type=a.get("type", "file"),
                name=a.get("name"),
                mime=a.get("mime"),
                size=a.get("size"),
                availability=a.get("availability", "placeholder"),
                local_ref=a.get("local_ref")
            )
            for a in raw_attachments
        ]
        is_self_val = None if r["is_self"] is None else (r["is_self"] == 1)

        return IMMessageItem(
            id=r["id"],
            ingest_seq=r["ingest_seq"],
            source=r["source"],
            account_id=r["account_id"],
            channel_id=r["channel_id"],
            source_id_quality=r["source_id_quality"],
            sender_id=r["sender_id"],
            sender_name=r["sender_name"],
            sender_role=r["sender_role"],
            is_self=is_self_val,
            reply_to=r["reply_to"],
            text=r["text"],
            message_type=r["message_type"],
            mentions=raw_mentions,
            attachments=attachments,
            occurred_at=r["occurred_at"],
            occurred_at_epoch_ms=r["occurred_at_epoch_ms"],
            observed_at=r["observed_at"],
            provenance=json.loads(r["provenance_json"]),
            focus_tags=json.loads(r["focus_tags_json"]),
            focus_reasons=json.loads(r["focus_reasons_json"]),
            source_message_id=r["source_message_id"]
        )
