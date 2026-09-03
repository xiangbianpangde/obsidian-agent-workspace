"""Vault scanner: walker -> exclude -> secret detector -> parser -> SQLite batch.
P1-M2-1: coordinator 原子化（begin_scan/end_scan/dispatch），事件在显式事务中重放。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..config import AppConfig
from ..database import sqlite
from ..security.path_guard import matches_scan_exclude
from ..security.secret_detector import looks_like_secret
from .parser import parse_markdown


def scan_vault(cfg: AppConfig, conn, *, coordinator=None) -> dict:
    """Full scan of vault into SQLite (single transaction). Returns stats."""
    if coordinator is not None:
        coordinator.begin_scan()
    t0 = time.time()
    run_id = sqlite.begin_scan(conn)

    exclude = cfg.scan_exclude
    vault_root = cfg.vault_root

    conn.execute("DELETE FROM files")
    conn.commit()

    n_files = 0
    n_secret = 0
    for dirpath, dirnames, filenames in os.walk(vault_root, followlinks=False):
        kept = []
        for d in dirnames:
            rel_dir = (Path(dirpath) / d).relative_to(vault_root).as_posix()
            if matches_scan_exclude(vault_root, rel_dir, exclude):
                sqlite.record_event(conn, "excluded", rel_dir)
                continue
            kept.append(d)
        dirnames[:] = kept

        for fname in filenames:
            if fname.startswith("."):
                continue
            if Path(fname).suffix.lower() != ".md":  # 只索引 Markdown
                continue
            full = Path(dirpath) / fname
            rel = full.relative_to(vault_root).as_posix()

            if cfg.reject_symlink_escape and full.is_symlink():
                try:
                    target = full.resolve(strict=True)
                except OSError:
                    target = Path("/")
                if not (target == vault_root or target.is_relative_to(vault_root)):
                    sqlite.record_event(conn, "excluded", rel, "symlink escape")
                    continue

            try:
                raw = full.read_bytes()
            except OSError as e:
                sqlite.record_event(conn, "excluded", rel, f"read error: {e}")
                continue

            hit, note = looks_like_secret(raw.decode("utf-8", errors="replace"))
            if hit:
                n_secret += 1
                sqlite.record_event(conn, "secret_skipped", rel, note)
                continue

            try:
                parsed = parse_markdown(full, vault_root, raw_bytes=raw)
            except Exception as e:  # noqa: BLE001
                sqlite.record_event(conn, "excluded", rel, f"parse error: {e}")
                continue

            _upsert_parsed(conn, parsed)
            n_files += 1

    duration_ms = int((time.time() - t0) * 1000)
    sqlite.finish_scan(conn, run_id, n_files, n_secret, duration_ms)
    conn.commit()
    if coordinator is not None:
        pending = coordinator.end_scan()
        for kind, rel in pending:  # replay 扫描期间积压事件
            process_event(cfg, conn, kind, rel)
    return {
        "run_id": run_id,
        "files_indexed": n_files,
        "secret_skipped": n_secret,
        "duration_ms": duration_ms,
        "stats": sqlite.stats(conn),
    }


def _upsert_parsed(conn, parsed) -> None:
    sqlite.upsert_file(
        conn,
        path=parsed.rel_path,
        filename=parsed.filename,
        title=parsed.title,
        folder=parsed.folder,
        size=parsed.size,
        created_at=parsed.created_at,
        modified_at=parsed.modified_at,
        sha256=parsed.sha256,
        tags=parsed.tags,
        metadata=parsed.metadata,
    )


def process_event(cfg: AppConfig, conn, kind: str, rel: str) -> None:
    """单事件处理（watchdog 与扫描后重放共用）。显式事务（P1-M2-1）。"""
    from ..security.path_guard import resolve_in_vault

    try:
        full = resolve_in_vault(cfg, rel)
    except Exception as e:  # noqa: BLE001
        sqlite.record_event(conn, "excluded", rel, f"path rejected: {e}")
        conn.commit()
        return

    if kind in ("moved_out", "deleted"):
        # MUST-1: 终态事件 replay 乱序防护 —— path 已重新存在则视为 stale，不盲删
        if full.exists() or full.is_symlink():
            sqlite.record_event(conn, "stale_terminal", rel, f"{kind} ignored, path re-created")
            conn.commit()
            return
        with sqlite.transaction(conn):
            sqlite.remove_file(conn, rel)
            sqlite.record_event(conn, "deleted", rel)
        return

    if not full.is_file() or full.suffix.lower() != ".md":
        return
    if matches_scan_exclude(cfg.vault_root, rel, cfg.scan_exclude):
        sqlite.record_event(conn, "excluded", rel)
        conn.commit()
        return

    hit, note = looks_like_secret(full.read_bytes().decode("utf-8", errors="replace"))
    if hit:
        with sqlite.transaction(conn):
            sqlite.remove_file(conn, rel)
            sqlite.record_event(conn, "secret_skipped", rel, note)
        return

    try:
        parsed = parse_markdown(full, cfg.vault_root)
        with sqlite.transaction(conn):
            _upsert_parsed(conn, parsed)
            sqlite.record_event(conn, kind, rel)
    except Exception as e:  # noqa: BLE001
        sqlite.record_event(conn, "excluded", rel, f"parse error: {e}")
        conn.commit()


def upsert_one(cfg: AppConfig, conn, rel_path: str, kind: str = "modified") -> None:
    process_event(cfg, conn, kind, rel_path)
