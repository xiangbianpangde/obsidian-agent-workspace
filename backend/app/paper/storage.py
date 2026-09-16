"""SQLite storage for the Paper Workbench.

Unlike the IM journal, this database is **not** a pure cache: ADR-007 gives it
authority over reading status, reading metadata and workspace state. It is
therefore operated as a state database:

* WAL journal mode, ``foreign_keys=ON``, explicit ``busy_timeout``;
* schema migrations tracked through ``PRAGMA user_version``;
* an append-only ``paper_status_events`` log so status history is auditable;
* soft deletion everywhere (ADR-002) — no physical ``DELETE`` statement appears
  in this module for papers, notes, sources or annotations.

Vault-owned data (identity, bindings, tags, notes, annotations) is mirrored here
only as a rebuildable index; the Vault remains the source of truth.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import (
    BindingState,
    Paper,
    PaperNote,
    PaperSource,
    PaperStatus,
    SourceRole,
    WorkspaceState,
    utc_now,
)

DEFAULT_PAPER_DIR = Path.home() / ".personal-ai-workspace" / "papers"
DEFAULT_DB_PATH = DEFAULT_PAPER_DIR / "papers.db"

#: Bump together with ``_MIGRATIONS``.
SCHEMA_VERSION = 1


def secure_harden_path(db_path: Path) -> None:
    os.umask(0o077)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(db_path.parent, 0o700)
    for path in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if path.exists():
            os.chmod(path, 0o600)


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id                  TEXT PRIMARY KEY,
    folder_relpath            TEXT NOT NULL,
    display_title             TEXT NOT NULL DEFAULT '',
    title_override            TEXT,
    category_relpath          TEXT NOT NULL DEFAULT '',
    manifest_relpath          TEXT,
    binding_state             TEXT NOT NULL DEFAULT 'DISCOVERED',
    primary_pdf_source_id     TEXT,
    primary_translation_source_id TEXT,
    note_id                   TEXT,
    paper_tags_json           TEXT NOT NULL DEFAULT '[]',
    external_ids_json         TEXT NOT NULL DEFAULT '{}',
    status                    TEXT NOT NULL DEFAULT 'UNREAD',
    first_opened_at           TEXT,
    last_opened_at            TEXT,
    completed_at              TEXT,
    status_changed_at         TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    inactive_at               TEXT
);

-- A folder holds at most one paper, and one paper_id must not appear twice:
-- duplicated folders with a copied manifest must fail closed (ADR-006).
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_folder
    ON papers(folder_relpath);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS idx_papers_binding_state ON papers(binding_state);
CREATE INDEX IF NOT EXISTS idx_papers_category ON papers(category_relpath);

CREATE TABLE IF NOT EXISTS paper_sources (
    source_id           TEXT PRIMARY KEY,
    paper_id            TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE RESTRICT,
    role                TEXT NOT NULL,
    media_kind          TEXT NOT NULL,
    rel_path            TEXT NOT NULL,
    rel_path_key_nfc    TEXT NOT NULL,
    is_primary          INTEGER NOT NULL DEFAULT 0,
    binding_origin      TEXT NOT NULL DEFAULT 'STRICT_RULE',
    binding_confidence  REAL,
    size_bytes          INTEGER,
    mtime_ns            INTEGER,
    sha256              TEXT,
    source_version      INTEGER NOT NULL DEFAULT 1,
    mime_type           TEXT NOT NULL DEFAULT '',
    language            TEXT,
    page_count          INTEGER,
    active              INTEGER NOT NULL DEFAULT 1,
    missing_since       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_paper_path
    ON paper_sources(paper_id, rel_path_key_nfc);
-- At most one primary per (paper, role family). Enforced in code too, because
-- SQLite cannot express a partial unique index over a derived family here.
CREATE INDEX IF NOT EXISTS idx_sources_paper_role ON paper_sources(paper_id, role);
CREATE INDEX IF NOT EXISTS idx_sources_sha ON paper_sources(sha256);

CREATE TABLE IF NOT EXISTS paper_notes (
    note_id          TEXT PRIMARY KEY,
    paper_id         TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE RESTRICT,
    rel_path         TEXT NOT NULL,
    content_sha256   TEXT,
    note_tags_json   TEXT NOT NULL DEFAULT '[]',
    schema_version   INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    missing_since    TEXT,
    inactive_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_notes_paper ON paper_notes(paper_id);

-- Rebuildable index over the authoritative Vault annotation sidecar (ADR-008).
-- Deleting rows here is always safe: the sidecar can regenerate them.
CREATE TABLE IF NOT EXISTS annotations_index (
    annotation_id          TEXT PRIMARY KEY,
    paper_id               TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE RESTRICT,
    source_id              TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    anchor_type            TEXT NOT NULL,
    page_index             INTEGER,
    heading_path_json      TEXT,
    selected_text          TEXT,
    body_markdown          TEXT,
    source_sha256          TEXT NOT NULL,
    source_version         INTEGER NOT NULL DEFAULT 1,
    orphaned_at            TEXT,
    deleted_at             TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotations_paper ON annotations_index(paper_id);
CREATE INDEX IF NOT EXISTS idx_annotations_source ON annotations_index(source_id);

CREATE TABLE IF NOT EXISTS workspace_states (
    paper_id                   TEXT PRIMARY KEY REFERENCES papers(paper_id) ON DELETE RESTRICT,
    active_pdf_source_id       TEXT,
    active_markdown_source_id  TEXT,
    active_pane                TEXT NOT NULL DEFAULT 'PDF',
    source_positions_json      TEXT NOT NULL DEFAULT '{}',
    note_id                    TEXT,
    note_cursor_start          INTEGER,
    note_cursor_end            INTEGER,
    note_content_sha256        TEXT,
    last_opened_at             TEXT,
    updated_at                 TEXT NOT NULL,
    state_version              INTEGER NOT NULL DEFAULT 1
);

-- Append-only status history for statistics and rollback auditing (ADR-007).
CREATE TABLE IF NOT EXISTS paper_status_events (
    event_id      TEXT PRIMARY KEY,
    paper_id      TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,
    reason        TEXT
);

CREATE INDEX IF NOT EXISTS idx_status_events_paper
    ON paper_status_events(paper_id, occurred_at);

-- Multi-file writes have no filesystem transaction. This table records intent
-- first so a crash mid-way can roll forward instead of leaving a half-created
-- note or manifest behind.
CREATE TABLE IF NOT EXISTS paper_write_intents (
    intent_id       TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    operation       TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TEXT NOT NULL,
    committed_at    TEXT,
    last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_write_intents_state
    ON paper_write_intents(state, created_at);
"""

#: Ordered migrations. Index 0 upgrades user_version 0 -> 1, and so on.
#: Each entry is (target_version, sql). Never edit a published entry; append.
_MIGRATIONS: List[tuple[int, str]] = [
    (1, _MIGRATION_1),
]


class PaperStorage:
    """Thread-safe SQLite access for the paper aggregate."""

    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = (
            Path(db_path).expanduser().resolve() if db_path else DEFAULT_DB_PATH
        )
        secure_harden_path(self.db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,  # explicit transaction management
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            current = int(cur.execute("PRAGMA user_version;").fetchone()[0])
            for target, sql in _MIGRATIONS:
                if target <= current:
                    continue
                # executescript() issues its own implicit COMMIT, so each
                # migration script carries its own BEGIN/COMMIT and the version
                # bump is appended to the same script to stay atomic.
                cur.executescript(
                    "BEGIN IMMEDIATE;\n" + sql + f"\nPRAGMA user_version={int(target)};\nCOMMIT;\n"
                )

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version;").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def backup_to(self, target: Path) -> Path:
        """Consistent snapshot via the SQLite backup API.

        Copying a WAL-mode database file directly can capture a torn state, so
        backups must go through this method (ADR-007).
        """
        target = Path(target).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            dest = sqlite3.connect(str(target))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        os.chmod(target, 0o600)
        return target

    # ------------------------------------------------------------------ papers
    def upsert_paper(self, paper: Paper, allow_folder_move: bool = False) -> bool:
        """Insert or update a paper.

        Returns ``True`` when the row was written, ``False`` when a conflicting
        folder binding blocked the write.

        A ``paper_id`` is bound to exactly one folder. Moving a folder is legal
        and must update the binding; copying a folder produces two live
        locations for one identity, which ADR-006 requires to **fail closed**
        rather than silently pick a winner. Callers must therefore opt into
        move semantics explicitly via ``allow_folder_move``.
        """
        row = paper.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "paper_id")
        with self._lock:
            if not allow_folder_move:
                existing = self._conn.execute(
                    "SELECT folder_relpath FROM papers WHERE paper_id = ?",
                    (paper.paper_id,),
                ).fetchone()
                if existing is not None and existing["folder_relpath"] != paper.folder_relpath:
                    # Same identity, different folder: a copy, not a move.
                    self._conn.execute(
                        "UPDATE papers SET binding_state = ? WHERE paper_id = ?",
                        (BindingState.DUPLICATE_ID_CONFLICT.value, paper.paper_id),
                    )
                    return False
            self._conn.execute(
                f"INSERT INTO papers ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(paper_id) DO UPDATE SET {updates}",
                list(row.values()),
            )
        return True

    def relocate_paper(
        self, paper_id: str, new_folder_relpath: str, category_relpath: str = ""
    ) -> None:
        """Rebind a paper after its folder was moved.

        Identity, status, annotations and workspace state all survive, which is
        the entire point of a path-independent ``paper_id`` (ADR-006).
        """
        stamp = utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET folder_relpath = ?, category_relpath = ?, "
                "updated_at = ? WHERE paper_id = ?",
                (new_folder_relpath, category_relpath, stamp, paper_id),
            )

    def get_paper(self, paper_id: str) -> Optional[Paper]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM papers WHERE paper_id = ?", (paper_id,)
            ).fetchone()
        return self._row_to_paper(row) if row else None

    def get_paper_by_folder(self, folder_relpath: str) -> Optional[Paper]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM papers WHERE folder_relpath = ?", (folder_relpath,)
            ).fetchone()
        return self._row_to_paper(row) if row else None

    def list_papers(
        self,
        status: Optional[PaperStatus] = None,
        category_relpath: Optional[str] = None,
        include_inactive: bool = False,
    ) -> List[Paper]:
        sql = "SELECT * FROM papers WHERE 1=1"
        params: List[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(PaperStatus.parse(status).value)
        if category_relpath is not None:
            sql += " AND category_relpath = ?"
            params.append(category_relpath)
        if not include_inactive:
            sql += " AND inactive_at IS NULL"
        sql += " ORDER BY display_title COLLATE NOCASE"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_paper(row) for row in rows]

    def count_papers(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE inactive_at IS NULL"
                ).fetchone()[0]
            )

    def find_duplicate_paper_ids(self) -> List[str]:
        """Paper IDs present in more than one folder.

        A copied folder carries its manifest along, producing two locations with
        one identity. ADR-006 requires failing closed rather than silently
        picking a winner.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT paper_id FROM papers GROUP BY paper_id HAVING COUNT(*) > 1"
            ).fetchall()
        return [row[0] for row in rows]

    def mark_paper_inactive(self, paper_id: str, when: Optional[str] = None) -> None:
        """Soft removal. Physical deletion of a paper is forbidden (ADR-002)."""
        stamp = when or utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET inactive_at = ?, updated_at = ? WHERE paper_id = ?",
                (stamp, stamp, paper_id),
            )

    # ------------------------------------------------------------------ status
    def set_status(
        self,
        paper_id: str,
        status: PaperStatus,
        reason: Optional[str] = None,
    ) -> None:
        """Transition status and append an audit event atomically.

        ``READING -> COMPLETED`` is a user decision only; callers must not infer
        it. ``UNREAD -> READING`` is triggered by a successful first load, not by
        a list click (ADR-007).
        """
        target = PaperStatus.parse(status)
        stamp = utc_now()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            try:
                row = cur.execute(
                    "SELECT status, first_opened_at FROM papers WHERE paper_id = ?",
                    (paper_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown paper: {paper_id}")
                previous = row["status"]
                first_opened = row["first_opened_at"]

                sets = ["status = ?", "status_changed_at = ?", "updated_at = ?"]
                params: List[Any] = [target.value, stamp, stamp]

                if target is PaperStatus.READING:
                    if not first_opened:
                        sets.append("first_opened_at = ?")
                        params.append(stamp)
                    sets.append("last_opened_at = ?")
                    params.append(stamp)
                elif target is PaperStatus.COMPLETED:
                    sets.append("completed_at = ?")
                    params.append(stamp)
                    sets.append("last_opened_at = ?")
                    params.append(stamp)

                params.append(paper_id)
                cur.execute(f"UPDATE papers SET {', '.join(sets)} WHERE paper_id = ?", params)

                cur.execute(
                    "INSERT INTO paper_status_events "
                    "(event_id, paper_id, from_status, to_status, occurred_at, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), paper_id, previous, target.value, stamp, reason),
                )
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def touch_last_opened(self, paper_id: str, when: Optional[str] = None) -> None:
        """Record that a paper was opened without changing its status.

        Re-opening a COMPLETED paper must not silently downgrade it.
        """
        stamp = when or utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET last_opened_at = ?, updated_at = ? WHERE paper_id = ?",
                (stamp, stamp, paper_id),
            )

    def list_status_events(self, paper_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_status_events WHERE paper_id = ? "
                "ORDER BY occurred_at",
                (paper_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------------- sources
    def upsert_source(self, source: PaperSource) -> None:
        row = source.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(
            f"{key}=excluded.{key}" for key in row if key != "source_id"
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO paper_sources ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(source_id) DO UPDATE SET {updates}",
                list(row.values()),
            )

    def list_sources(self, paper_id: str, include_inactive: bool = False) -> List[PaperSource]:
        sql = "SELECT * FROM paper_sources WHERE paper_id = ?"
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY role"
        with self._lock:
            rows = self._conn.execute(sql, (paper_id,)).fetchall()
        return [self._row_to_source(row) for row in rows]

    def get_source(self, source_id: str) -> Optional[PaperSource]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM paper_sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        return self._row_to_source(row) if row else None

    def find_sources_by_sha(self, sha256: str) -> List[PaperSource]:
        """Used for rename recovery and duplicate-candidate hints only.

        A content hash is never an identity (ADR-006).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_sources WHERE sha256 = ? AND active = 1",
                (sha256,),
            ).fetchall()
        return [self._row_to_source(row) for row in rows]

    def deactivate_source(self, source_id: str) -> None:
        """Supersede a binding without deleting it (zero-delete)."""
        stamp = utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE paper_sources SET active = 0, updated_at = ? WHERE source_id = ?",
                (stamp, source_id),
            )

    def mark_sources_missing(self, paper_id: str, missing: Iterable[str]) -> None:
        """Record that bound files vanished externally (tombstone, never delete)."""
        stamp = utc_now()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            try:
                cur.execute(
                    "UPDATE paper_sources SET missing_since = ?, updated_at = ? "
                    "WHERE paper_id = ? AND missing_since IS NULL",
                    (stamp, stamp, paper_id),
                )
                for rel_path in missing:
                    cur.execute(
                        "UPDATE paper_sources SET missing_since = ?, updated_at = ? "
                        "WHERE paper_id = ? AND rel_path = ?",
                        (stamp, stamp, paper_id, rel_path),
                    )
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    # ------------------------------------------------------------------- notes
    def upsert_note(self, note: PaperNote) -> None:
        row = note.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "note_id")
        with self._lock:
            self._conn.execute(
                f"INSERT INTO paper_notes ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(note_id) DO UPDATE SET {updates}",
                list(row.values()),
            )

    def get_note(self, note_id: str) -> Optional[PaperNote]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM paper_notes WHERE note_id = ?", (note_id,)
            ).fetchone()
        return self._row_to_note(row) if row else None

    def get_note_for_paper(self, paper_id: str) -> Optional[PaperNote]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM paper_notes WHERE paper_id = ? AND inactive_at IS NULL",
                (paper_id,),
            ).fetchone()
        return self._row_to_note(row) if row else None

    def mark_note_missing(self, note_id: str, when: Optional[str] = None) -> None:
        stamp = when or utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE paper_notes SET missing_since = ?, updated_at = ? WHERE note_id = ?",
                (stamp, stamp, note_id),
            )

    # ------------------------------------------------------------- annotations
    def replace_annotations_index(
        self, paper_id: str, rows: List[Dict[str, Any]]
    ) -> None:
        """Rebuild the annotation index for one paper atomically.

        Safe to delete from: this table is a derived index over the
        authoritative Vault sidecar (ADR-008).
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            try:
                cur.execute("DELETE FROM annotations_index WHERE paper_id = ?", (paper_id,))
                for row in rows:
                    cur.execute(
                        "INSERT INTO annotations_index ("
                        "annotation_id, paper_id, source_id, kind, anchor_type, "
                        "page_index, heading_path_json, selected_text, body_markdown, "
                        "source_sha256, source_version, orphaned_at, deleted_at, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["annotation_id"],
                            paper_id,
                            row["source_id"],
                            row["kind"],
                            row["anchor_type"],
                            row.get("page_index"),
                            row.get("heading_path_json"),
                            row.get("selected_text"),
                            row.get("body_markdown"),
                            row["source_sha256"],
                            row.get("source_version", 1),
                            row.get("orphaned_at"),
                            row.get("deleted_at"),
                            row["created_at"],
                            row["updated_at"],
                        ),
                    )
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def list_annotations(
        self, paper_id: str, include_deleted: bool = False
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM annotations_index WHERE paper_id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(sql, (paper_id,)).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- workspace
    def get_workspace_state(self, paper_id: str) -> Optional[WorkspaceState]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workspace_states WHERE paper_id = ?", (paper_id,)
            ).fetchone()
        if row is None:
            return None
        return WorkspaceState(
            paper_id=row["paper_id"],
            active_pdf_source_id=row["active_pdf_source_id"],
            active_markdown_source_id=row["active_markdown_source_id"],
            active_pane=row["active_pane"],
            source_positions=json.loads(row["source_positions_json"] or "{}"),
            note_id=row["note_id"],
            note_cursor_start=row["note_cursor_start"],
            note_cursor_end=row["note_cursor_end"],
            note_content_sha256=row["note_content_sha256"],
            last_opened_at=row["last_opened_at"],
            updated_at=row["updated_at"],
            state_version=row["state_version"],
        )

    def upsert_workspace_state(self, state: WorkspaceState) -> None:
        row = state.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(
            f"{key}=excluded.{key}" for key in row if key != "paper_id"
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO workspace_states ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(paper_id) DO UPDATE SET {updates}",
                list(row.values()),
            )

    # ------------------------------------------------------------ write intent
    def begin_write_intent(
        self, paper_id: str, operation: str, payload: Optional[Dict[str, Any]] = None
    ) -> str:
        """Record intent before a multi-file Vault write.

        The filesystem has no cross-file transaction, so a crash between
        "create note" and "update manifest" is recovered by rolling forward from
        this record — never by deleting the note that was already created.
        """
        intent_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO paper_write_intents "
                "(intent_id, paper_id, operation, payload_json, state, created_at) "
                "VALUES (?, ?, ?, ?, 'PENDING', ?)",
                (
                    intent_id,
                    paper_id,
                    operation,
                    json.dumps(payload or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
        return intent_id

    def commit_write_intent(self, intent_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE paper_write_intents SET state='COMMITTED', committed_at=? "
                "WHERE intent_id = ?",
                (utc_now(), intent_id),
            )

    def fail_write_intent(self, intent_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE paper_write_intents SET state='FAILED', last_error=? "
                "WHERE intent_id = ?",
                (error[:2000], intent_id),
            )

    def list_pending_write_intents(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_write_intents WHERE state='PENDING' "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row_to_paper(row: sqlite3.Row) -> Paper:
        return Paper(
            paper_id=row["paper_id"],
            folder_relpath=row["folder_relpath"],
            display_title=row["display_title"],
            title_override=row["title_override"],
            category_relpath=row["category_relpath"],
            manifest_relpath=row["manifest_relpath"],
            binding_state=BindingState(row["binding_state"]),
            primary_pdf_source_id=row["primary_pdf_source_id"],
            primary_translation_source_id=row["primary_translation_source_id"],
            note_id=row["note_id"],
            paper_tags=json.loads(row["paper_tags_json"] or "[]"),
            external_ids=json.loads(row["external_ids_json"] or "{}"),
            status=PaperStatus(row["status"]),
            first_opened_at=row["first_opened_at"],
            last_opened_at=row["last_opened_at"],
            completed_at=row["completed_at"],
            status_changed_at=row["status_changed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            inactive_at=row["inactive_at"],
        )

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> PaperSource:
        return PaperSource(
            source_id=row["source_id"],
            paper_id=row["paper_id"],
            role=SourceRole(row["role"]),
            rel_path=row["rel_path"],
            rel_path_key_nfc=row["rel_path_key_nfc"],
            is_primary=bool(row["is_primary"]),
            binding_origin=row["binding_origin"],
            binding_confidence=row["binding_confidence"],
            size_bytes=row["size_bytes"],
            mtime_ns=row["mtime_ns"],
            sha256=row["sha256"],
            source_version=row["source_version"],
            mime_type=row["mime_type"],
            language=row["language"],
            page_count=row["page_count"],
            active=bool(row["active"]),
            missing_since=row["missing_since"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> PaperNote:
        return PaperNote(
            note_id=row["note_id"],
            paper_id=row["paper_id"],
            rel_path=row["rel_path"],
            content_sha256=row["content_sha256"],
            note_tags=json.loads(row["note_tags_json"] or "[]"),
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            missing_since=row["missing_since"],
            inactive_at=row["inactive_at"],
        )
