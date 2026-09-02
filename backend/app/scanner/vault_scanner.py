"""Vault scanner: walker -> exclude -> secret detector -> parser -> SQLite batch."""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..config import AppConfig
from ..database import sqlite
from ..security.secret_detector import looks_like_secret
from .parser import parse_markdown


def _excluded(rel_or_name: str, exclude: list[str]) -> bool:
    if not exclude:
        return False
    parts = Path(rel_or_name).parts
    for token in exclude:
        if parts and token in parts:
            return True
        if token.startswith("."):
            # 隐藏配置目录：部分出现在深层也是排除
            if any(p.startswith(".obsidian") or p == token for p in parts):
                return True
    return False


def scan_vault(cfg: AppConfig, conn, *, record_secrets: bool = True,
               coordinator=None) -> dict:
    """Full scan of vault into SQLite (single transaction). Returns stats.
    Sol M1-B: 可选 coordinator —— 扫描期间 watchdog 事件入队，结束后 replay。"""
    if coordinator is not None:
        coordinator.acquire()
    t0 = time.time()
    run_id = sqlite.begin_scan(conn)

    exclude = cfg.scan_exclude
    ext_ignore = {e.lower() for e in cfg.extension_ignore}
    vault_root = cfg.vault_root

    # 全量重建索引（path 稳定；tombstone 由 index_events 保留）
    conn.execute("DELETE FROM files")
    conn.commit()

    n_files = 0
    n_secret = 0
    for dirpath, dirnames, filenames in os.walk(vault_root, followlinks=False):
        # 剪枝排除目录（原地修改 dirnames）
        kept = []
        for d in dirnames:
            if _excluded((Path(dirpath) / d).relative_to(vault_root).as_posix(), exclude):
                sqlite.record_event(
                    conn, "excluded", str(Path(dirpath).relative_to(vault_root) / d)
                )
                continue
            kept.append(d)
        dirnames[:] = kept

        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = Path(fname).suffix.lower()
            if ext != ".md":            # 只索引 Markdown（v0.2 §1 P0-1）；大文件非 md 直接跳过
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

            # secret detector: 第一层，永不进入 parser（v0.2 §7.5）
            hit, note = looks_like_secret(raw.decode("utf-8", errors="replace"))
            if hit:
                n_secret += 1
                sqlite.record_event(conn, "secret_skipped", rel, note)
                continue

            try:
                parsed = parse_markdown(full, vault_root)
            except Exception as e:  # noqa: BLE001 - 单文件失败不阻断全量
                sqlite.record_event(conn, "excluded", rel, f"parse error: {e}")
                continue

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
            n_files += 1

    duration_ms = int((time.time() - t0) * 1000)
    sqlite.finish_scan(conn, run_id, n_files, n_secret, duration_ms)
    conn.commit()
    if coordinator is not None:
        coordinator.release()
        for kind, rel in coordinator.drain():   # replay 扫描期间积压事件
            upsert_one(cfg, conn, rel, kind=kind)
    return {
        "run_id": run_id,
        "files_indexed": n_files,
        "secret_skipped": n_secret,
        "duration_ms": duration_ms,
        "stats": sqlite.stats(conn),
    }


def upsert_one(cfg: AppConfig, conn, rel_path: str, kind: str = "modified") -> None:
    """Watchdog 增量：单文件 upsert（先过排除与 secret 检测）。"""
    from ..security.path_guard import resolve_in_vault

    try:
        full = resolve_in_vault(cfg, rel_path)
    except Exception as e:  # noqa: BLE001
        sqlite.record_event(conn, "excluded", rel_path, f"path rejected: {e}")
        return
    if not full.is_file():
        return
    if full.suffix.lower() != ".md":
        return  # watchdog 的非 md 事件忽略（大文件等）

    hit, note = looks_like_secret(full.read_bytes().decode("utf-8", errors="replace"))
    if hit:
        sqlite.remove_file(conn, rel_path)
        sqlite.record_event(conn, "secret_skipped", rel_path, note)
        conn.commit()
        return

    try:
        parsed = parse_markdown(full, cfg.vault_root)
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
        sqlite.record_event(conn, kind, rel_path)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        sqlite.record_event(conn, "excluded", rel_path, f"parse error: {e}")
        conn.commit()
