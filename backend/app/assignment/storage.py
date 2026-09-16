"""Local-first encrypted storage for platform credentials & task snapshots.

铁律：
- 凭证 Fernet 加密落库，密钥存同目录独立 key 文件（0600）
- 目录 0700 / db 0600 / WAL+SHM 0600（复用 schedule 的 secure_harden_path 模式）
- 零删除：任务失效只标 is_stale；凭证"禁用"只标 is_disabled
- UNIQUE(platform, external_id) 去重，插入/更新不覆盖 first_seen_at
- 全部 SQL 为单行字符串字面量 + 占位符参数（Mimosa 行级扫描友好；ruff 忽略 E501）
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .guard import GuardError
from .models import NormalizedTask

DEFAULT_DATA_DIR = Path.home() / ".personal-ai-workspace" / "assignment"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "assignment_platforms.db"
KEY_FILENAME = "credential.key"


def secure_harden_path(db_path: Path) -> None:
    os.umask(0o077)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(db_path.parent, 0o700)
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            os.chmod(p, 0o600)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class CredentialVault:
    """Fernet encryption helper; key file created once beside the DB."""

    def __init__(self, key_path: Path):
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
            key_path.chmod(0o600)
        else:
            key_path.chmod(0o600)
        self._fernet = Fernet(key_path.read_bytes())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise GuardError("credential decryption failed (key mismatch?)") from exc


class AssignmentStorage:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path).expanduser().resolve() if db_path else DEFAULT_DB_PATH
        secure_harden_path(self.db_path)
        self._lock = threading.RLock()
        self._vault = CredentialVault(self.db_path.parent / KEY_FILENAME)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS platform_credentials (platform TEXT PRIMARY KEY CHECK(platform IN ('chaoxing','smartestu')), credential BLOB NOT NULL, is_disabled INTEGER NOT NULL DEFAULT 0, last_validated_at INTEGER, last_validation_ok INTEGER, updated_at INTEGER NOT NULL);"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS platform_tasks (platform TEXT NOT NULL CHECK(platform IN ('chaoxing','smartestu')), external_id TEXT NOT NULL, course_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, due_at INTEGER, status TEXT NOT NULL DEFAULT 'unknown', score REAL, detail_url TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '{}', first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, is_stale INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (platform, external_id));"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_due ON platform_tasks (due_at) WHERE is_stale = 0;"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sync_journal (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER, ok INTEGER NOT NULL DEFAULT 0, task_count INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '');"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- credentials
    def save_credential(self, platform: str, plaintext: str) -> None:
        blob = self._vault.encrypt(plaintext)
        with self._lock:
            self._conn.execute(
                "INSERT INTO platform_credentials (platform, credential, updated_at) VALUES (?, ?, ?) ON CONFLICT(platform) DO UPDATE SET credential = excluded.credential, is_disabled = 0, updated_at = excluded.updated_at;",
                (platform, blob, _now_ms()),
            )

    def disable_credential(self, platform: str) -> bool:
        """Zero-delete: mark disabled instead of deleting the row."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE platform_credentials SET is_disabled = 1, updated_at = ? WHERE platform = ?;",
                (_now_ms(), platform),
            )
            return cur.rowcount > 0

    def get_credential(self, platform: str) -> str | None:
        """Returns decrypted plaintext credential, or None if absent/disabled."""
        with self._lock:
            row = self._conn.execute(
                "SELECT credential, is_disabled FROM platform_credentials WHERE platform = ?;",
                (platform,),
            ).fetchone()
        if row is None or row["is_disabled"]:
            return None
        return self._vault.decrypt(row["credential"])

    def mark_validation(self, platform: str, ok: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE platform_credentials SET last_validated_at = ?, last_validation_ok = ? WHERE platform = ?;",
                (_now_ms(), 1 if ok else 0, platform),
            )

    def credential_status(self) -> list[dict[str, Any]]:
        """Per-platform status WITHOUT ever returning the credential itself."""
        out: list[dict[str, Any]] = []
        for platform in ("chaoxing", "smartestu"):
            with self._lock:
                row = self._conn.execute(
                    "SELECT last_validated_at, last_validation_ok, is_disabled, updated_at FROM platform_credentials WHERE platform = ?;",
                    (platform,),
                ).fetchone()
            out.append(
                {
                    "platform": platform,
                    "configured": row is not None and not row["is_disabled"],
                    "last_validated_at": row["last_validated_at"] if row else None,
                    "last_validation_ok": bool(row["last_validation_ok"]) if row else None,
                    "is_disabled": bool(row["is_disabled"]) if row else False,
                }
            )
        return out

    def credential_preview(self, platform: str) -> str | None:
        """Masked preview for the UI (first 6 chars + length), never the full value."""
        plain = self.get_credential(platform)
        if plain is None:
            return None
        return plain[:6] + "…(" + str(len(plain)) + " chars)"

    # ------------------------------------------------------------------- tasks
    def upsert_tasks(self, tasks: list[NormalizedTask]) -> dict[str, int]:
        now = _now_ms()
        inserted = updated = 0
        with self._lock:
            self._conn.execute("BEGIN TRANSACTION;")
            try:
                for t in tasks:
                    errors = t.validate()
                    if errors:
                        raise ValueError("invalid task: " + "; ".join(errors))
                    due_ms = int(t.due_at.timestamp() * 1000) if t.due_at else None
                    fetched_ms = int(t.fetched_at.timestamp() * 1000) if t.fetched_at else now
                    existed = self._conn.execute(
                        "SELECT 1 FROM platform_tasks WHERE platform = ? AND external_id = ?;",
                        (t.platform, t.external_id),
                    ).fetchone()
                    self._conn.execute(
                        "INSERT INTO platform_tasks (platform, external_id, course_name, title, due_at, status, score, detail_url, raw, first_seen_at, last_seen_at, is_stale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(platform, external_id) DO UPDATE SET course_name = excluded.course_name, title = excluded.title, due_at = excluded.due_at, status = excluded.status, score = excluded.score, detail_url = excluded.detail_url, raw = excluded.raw, last_seen_at = excluded.last_seen_at, is_stale = 0;",
                        (
                            t.platform,
                            t.external_id,
                            t.course_name,
                            t.title,
                            due_ms,
                            t.status,
                            t.score,
                            t.detail_url,
                            json.dumps(t.raw, ensure_ascii=False),
                            fetched_ms,
                            now,
                        ),
                    )
                    if existed:
                        updated += 1
                    else:
                        inserted += 1
                self._conn.execute("COMMIT;")
            except Exception:
                self._conn.execute("ROLLBACK;")
                raise
        return {"inserted": inserted, "updated": updated, "total": len(tasks)}

    def mark_stale_except(self, platform: str, seen_external_ids: list[str]) -> int:
        """Tasks of `platform` not in the latest sync batch are marked stale.

        两阶段静态 SQL：先全标 stale，再把本批 seen 的逐条恢复，避免动态 IN 列表。
        """
        seen = set(seen_external_ids)
        with self._lock:
            self._conn.execute("BEGIN TRANSACTION;")
            try:
                self._conn.execute(
                    "UPDATE platform_tasks SET is_stale = 1 WHERE platform = ? AND is_stale = 0;",
                    (platform,),
                )
                rows = self._conn.execute(
                    "SELECT external_id FROM platform_tasks WHERE platform = ?;", (platform,)
                ).fetchall()
                recovered = 0
                for r in rows:
                    if r["external_id"] in seen:
                        self._conn.execute(
                            "UPDATE platform_tasks SET is_stale = 0 WHERE platform = ? AND external_id = ?;",
                            (platform, r["external_id"]),
                        )
                        recovered += 1
                self._conn.execute("COMMIT;")
            except Exception:
                self._conn.execute("ROLLBACK;")
                raise
        return len(rows) - recovered

    def list_tasks(
        self,
        platform: str | None = None,
        status: str | None = None,
        due_before_ms: int | None = None,
        include_stale: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        # 全静态查询：NULL/布尔哨兵参数 + 占位符，避免任何语句组装
        params: list[Any] = [
            platform,
            platform,
            status,
            status,
            due_before_ms,
            due_before_ms,
            1 if include_stale else 0,
            limit,
        ]
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM platform_tasks WHERE (? IS NULL OR platform = ?) AND (? IS NULL OR status = ?) AND (? IS NULL OR (due_at IS NOT NULL AND due_at <= ?)) AND (? = 1 OR is_stale = 0) ORDER BY (due_at IS NULL), due_at ASC, last_seen_at DESC LIMIT ?;",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- sync journal
    def start_sync(self, platform: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sync_journal (platform, started_at) VALUES (?, ?);",
                (platform, _now_ms()),
            )
            return int(cur.lastrowid or 0)

    def finish_sync(self, sync_id: int, ok: bool, task_count: int, message: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sync_journal SET finished_at = ?, ok = ?, task_count = ?, message = ? WHERE id = ?;",
                (_now_ms(), 1 if ok else 0, task_count, message[:500], sync_id),
            )

    def last_sync(self, platform: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if platform:
                row = self._conn.execute(
                    "SELECT * FROM sync_journal WHERE platform = ? ORDER BY id DESC LIMIT 1;",
                    (platform,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM sync_journal ORDER BY id DESC LIMIT 1;"
                ).fetchone()
        return dict(row) if row else None
