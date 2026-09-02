"""SQLite index: files / tags / file_tags / metadata + scan_runs / index_events."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    title TEXT,
    folder TEXT,
    size INTEGER,
    created_at TEXT,
    modified_at TEXT,
    hash TEXT,               -- sha256(content)  (P0-MUST-3)
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
CREATE INDEX IF NOT EXISTS idx_files_modified ON files(modified_at);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);

CREATE TABLE IF NOT EXISTS file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id);

CREATE TABLE IF NOT EXISTS metadata (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT,
    value_type TEXT NOT NULL DEFAULT 'string',  -- string|number|bool|list|yaml
    PRIMARY KEY (file_id, key)
);
CREATE INDEX IF NOT EXISTS idx_metadata_key ON metadata(key);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    files_indexed INTEGER,
    files_skipped_secret INTEGER,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS index_events (      -- tombstone / secret-skip audit (v0.1 §7.2 DELETE 只记录)
    id INTEGER PRIMARY KEY,
    ts TEXT,
    kind TEXT,              -- created|modified|moved|deleted|secret_skipped|excluded
    path TEXT,
    note TEXT
);
"""

_VALUE_TYPES = ("string", "number", "bool", "list", "yaml")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)  # FastAPI threadpool + watchdog 线程
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def classify_value(value: Any) -> tuple[str, str]:
    """frontmatter value -> (value_type, text).  (Sol: string|number|bool|list|yaml)"""
    if value is None:
        return "string", ""
    if isinstance(value, bool):
        return "bool", "true" if value else "false"
    if isinstance(value, (int, float)):
        return "number", str(value)
    if isinstance(value, str):
        return "string", value
    if isinstance(value, (list, tuple)):
        import json

        return "list", json.dumps(list(value), ensure_ascii=False)
    import yaml as _yaml

    try:
        return "yaml", _yaml.safe_dump(value, allow_unicode=True).strip()
    except Exception:
        return "yaml", str(value)


def upsert_file(
    conn: sqlite3.Connection,
    *,
    path: str,
    filename: str,
    title: str | None,
    folder: str | None,
    size: int,
    created_at: str | None,
    modified_at: str | None,
    sha256: str,
    tags: list[str],
    metadata: dict[str, tuple[str, str]],
) -> int:
    """Insert or replace one file row + relations. Returns file_id."""
    cur = conn.execute(
        """
        INSERT INTO files(path, filename, title, folder, size, created_at, modified_at, hash, indexed_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
          filename=excluded.filename, title=excluded.title, folder=excluded.folder,
          size=excluded.size, created_at=excluded.created_at, modified_at=excluded.modified_at,
          hash=excluded.hash, indexed_at=excluded.indexed_at
        """,
        (path, filename, title, folder, size, created_at, modified_at, sha256, now_iso()),
    )
    row = conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
    file_id = row["id"] if row else cur.lastrowid

    conn.execute("DELETE FROM file_tags WHERE file_id=?", (file_id,))
    conn.execute("DELETE FROM metadata WHERE file_id=?", (file_id,))
    for tag in tags:
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (tag,))
        tag_row = conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO file_tags(file_id, tag_id) VALUES(?,?)",
            (file_id, tag_row["id"]),
        )
    for key, (value, vtype) in metadata.items():
        conn.execute(
            "INSERT OR REPLACE INTO metadata(file_id, key, value, value_type) VALUES(?,?,?,?)",
            (file_id, key, value, vtype),
        )
    return file_id


def remove_file(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM files WHERE path=?", (path,))


def record_event(
    conn: sqlite3.Connection, kind: str, path: str, note: str = ""
) -> None:
    conn.execute(
        "INSERT INTO index_events(ts, kind, path, note) VALUES(?,?,?,?)",
        (now_iso(), kind, path, note),
    )


def begin_scan(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO scan_runs(started_at) VALUES(?)", (now_iso(),)
    )
    return cur.lastrowid


def finish_scan(
    conn: sqlite3.Connection,
    run_id: int,
    files_indexed: int,
    files_skipped_secret: int,
    duration_ms: int,
) -> None:
    conn.execute(
        "UPDATE scan_runs SET finished_at=?, files_indexed=?, files_skipped_secret=?, duration_ms=? WHERE id=?",
        (now_iso(), files_indexed, files_skipped_secret, duration_ms, run_id),
    )


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "files": conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"],
        "tags": conn.execute("SELECT COUNT(*) c FROM tags").fetchone()["c"],
        "metadata_rows": conn.execute("SELECT COUNT(*) c FROM metadata").fetchone()["c"],
        "events": conn.execute("SELECT COUNT(*) c FROM index_events").fetchone()["c"],
        "last_scan": conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone(),
    }
