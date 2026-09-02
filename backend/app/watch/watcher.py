"""Watchdog: 增量监听 CREATE/MODIFY/MOVE；DELETE 只记录 tombstone（v0.2 §7 / v0.1 §7.2）。
Sol M1-A：per-path debounce；Sol M1-B：与全量扫描互斥（扫描期间事件入队）。"""
from __future__ import annotations

import threading
import time
from collections import deque

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..config import AppConfig
from ..database import sqlite
from ..scanner.vault_scanner import _excluded, upsert_one


class ScanCoordinator:
    """全量扫描与 watchdog 的互斥协调器（Sol M1-B）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: deque = deque()

    @property
    def scanning(self) -> bool:
        return self._lock.locked()

    def acquire(self) -> None:
        self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def queue(self, item: tuple[str, str]) -> None:
        if len(self._pending) < 5000:
            self._pending.append(item)

    def drain(self) -> list[tuple[str, str]]:
        items = list(self._pending)
        self._pending.clear()
        return items


class VaultEventHandler(FileSystemEventHandler):
    def __init__(self, cfg: AppConfig, conn, debounce_ms: int = 500,
                 coordinator: ScanCoordinator | None = None):
        self.cfg = cfg
        self.conn = conn
        self.debounce_ms = debounce_ms
        self.coordinator = coordinator
        self._last_by_path: dict[str, float] = {}

    def _debounce(self, rel: str) -> bool:
        now = time.time()
        last = self._last_by_path.get(rel, 0.0)
        if now - last < self.debounce_ms / 1000:
            self._last_by_path[rel] = now
            return True
        self._last_by_path[rel] = now
        return False

    @staticmethod
    def _rel(path: str) -> str:
        # path 已是 event.src_path；只取相对部分（绝对路径拆分由 observer 保证在 vault 内）
        return str(path)

    def _handle(self, kind: str, rel: str) -> None:
        if _excluded(rel, self.cfg.scan_exclude):
            sqlite.record_event(self.conn, "excluded", rel)
            self.conn.commit()
            return
        if self._debounce(rel):
            return
        if self.coordinator is not None and self.coordinator.scanning:
            self.coordinator.queue((kind, rel))
            return
        upsert_one(self.cfg, self.conn, rel, kind=kind)

    def on_created(self, event):
        if not event.is_directory:
            self._handle("created", _rel_to_vault(self.cfg, event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self._handle("modified", _rel_to_vault(self.cfg, event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._handle("moved", _rel_to_vault(self.cfg, event.dest_path))
            sqlite.remove_file(self.conn, _rel_to_vault(self.cfg, event.src_path))
            sqlite.record_event(
                self.conn, "moved", _rel_to_vault(self.cfg, event.src_path),
                "->" + _rel_to_vault(self.cfg, event.dest_path),
            )
            self.conn.commit()

    def on_deleted(self, event):
        if not event.is_directory:
            rel = _rel_to_vault(self.cfg, event.src_path)
            sqlite.remove_file(self.conn, rel)
            sqlite.record_event(self.conn, "deleted", rel)
            self.conn.commit()


def _rel_to_vault(cfg: AppConfig, src_path: str) -> str:
    from pathlib import Path as _P

    p = _P(src_path)
    try:
        return str(p.relative_to(cfg.vault_root))
    except ValueError:
        return p.name


def start_watcher(cfg: AppConfig, conn, coordinator: ScanCoordinator | None = None):
    handler = VaultEventHandler(cfg, conn, cfg.debounce_ms, coordinator)
    observer = Observer()
    observer.schedule(handler, str(cfg.vault_root), recursive=True)
    observer.start()
    return observer
