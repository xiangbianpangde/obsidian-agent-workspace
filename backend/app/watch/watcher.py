"""Watchdog: 增量监听 CREATE/MODIFY/MOVE；DELETE 只记录 tombstone（v0.2 §7 / v0.1 §7.2）。"""
from __future__ import annotations

import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..config import AppConfig
from ..database import sqlite
from .vault_scanner import _excluded, upsert_one


class VaultEventHandler(FileSystemEventHandler):
    def __init__(self, cfg: AppConfig, conn, debounce_ms: int = 500):
        self.cfg = cfg
        self.conn = conn
        self.debounce_ms = debounce_ms
        self._last = {"kind": "", "path": "", "ts": 0.0}

    def _debounce(self, kind: str, rel: str) -> bool:
        now = time.time()
        if (
            now - self._last["ts"] < self.debounce_ms / 1000
            and rel == self._last["path"]
        ):
            self._last["ts"] = now
            return True
        self._last = {"kind": kind, "path": rel, "ts": now}
        return False

    def _rel(self, path: str) -> str:
        return str((self.cfg.vault_root / path).relative_to(self.cfg.vault_root))

    def _handle(self, kind: str, path: str) -> None:
        rel = self._rel(path)
        if _excluded(rel, self.cfg.scan_exclude):
            sqlite.record_event(self.conn, "excluded", rel)
            self.conn.commit()
            return
        if self._debounce(kind, rel):
            return
        upsert_one(self.cfg, self.conn, rel, kind=kind)

    def on_created(self, event):
        if not event.is_directory:
            self._handle("created", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle("modified", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle("moved", event.dest_path)
            # 旧路径 tombstone
            sqlite.remove_file(self.conn, self._rel(event.src_path))
            sqlite.record_event(self.conn, "moved", self._rel(event.src_path), "->" + self._rel(event.dest_path))
            self.conn.commit()

    def on_deleted(self, event):
        if not event.is_directory:
            rel = self._rel(event.src_path)
            sqlite.remove_file(self.conn, rel)
            sqlite.record_event(self.conn, "deleted", rel)
            self.conn.commit()


def start_watcher(cfg: AppConfig, conn):
    handler = VaultEventHandler(cfg, conn, cfg.debounce_ms)
    observer = Observer()
    observer.schedule(handler, str(cfg.vault_root), recursive=True)
    observer.start()
    return observer
