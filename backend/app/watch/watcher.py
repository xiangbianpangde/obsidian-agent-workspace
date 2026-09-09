"""Watchdog: 增量监听 CREATE/MODIFY/MOVE；DELETE 只记录 tombstone（v0.2 §7）。
P1-M2-1: 专用 connection + 原子 coordinator（CREATE/MODIFY/MOVE/DELETE 全部过协调）+ per-path debounce。"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path as _P

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..config import AppConfig
from ..scanner.vault_scanner import process_event

logger = logging.getLogger(__name__)


class ScanCoordinator:
    """全量扫描与事件处理的原子互斥（P1-M2-1：check→queue 无竞态）。"""

    def __init__(self):
        self._mutex = threading.Lock()
        self._event_lock = (
            threading.Lock()
        )  # Sol P2: 序列化事件处理与扫描起点，杜绝 check→process 窗口与扫描交错
        self._scanning = False
        self._pending: deque = deque()

    def begin_scan(self) -> None:
        with self._event_lock:
            with self._mutex:
                self._scanning = True

    def end_scan(self) -> list[tuple[str, str]]:
        with self._mutex:
            self._scanning = False
            items = list(self._pending)
            self._pending.clear()
        return items

    def dispatch(self, event: tuple[str, str]) -> bool:
        """返回 True = 调用方立即处理；False = 已入队（扫描结束后 replay）。"""
        with self._mutex:
            if self._scanning:
                if len(self._pending) < 5000:
                    self._pending.append(event)
                return False
        return True

    def run_exclusive(self, fn) -> None:
        """在事件互斥锁内执行处理函数：watchdog 单事件与 begin_scan 不再交错。"""
        with self._event_lock:
            fn()


class VaultEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        cfg: AppConfig,
        conn,
        debounce_ms: int = 500,
        coordinator: ScanCoordinator | None = None,
    ):
        self.cfg = cfg
        self.conn = conn
        self.debounce_ms = debounce_ms
        self.coordinator = coordinator
        # LRU 限容去抖表（Sol P2：原 dict 只增不减，长期运行内存无界增长）
        self._last_by_path: OrderedDict[str, float] = OrderedDict()
        self._debounce_max = 4096

    def _debounce(self, rel: str) -> bool:
        now = time.time()
        last = self._last_by_path.get(rel, 0.0)
        if now - last < self.debounce_ms / 1000:
            self._last_by_path[rel] = now
            self._last_by_path.move_to_end(rel)
            return True
        self._last_by_path[rel] = now
        self._last_by_path.move_to_end(rel)
        if len(self._last_by_path) > self._debounce_max:
            self._last_by_path.popitem(last=False)
        return False

    def _emit(self, kind: str, rel: str) -> None:
        if not rel or rel.startswith("."):
            return
        if matches_exclude(self.cfg, rel):
            return
        # MUST-1: 仅 modified 参与 debounce；created/moved_in/deleted/moved_out 均始终处理，防止正向 identity event 被旧 modify 吞掉
        if kind == "modified" and self._debounce(rel):
            return
        if self.coordinator is not None:
            if not self.coordinator.dispatch((kind, rel)):
                return
            try:
                self.coordinator.run_exclusive(
                    lambda k=kind, r=rel: process_event(self.cfg, self.conn, k, r)
                )
            except Exception:  # noqa: BLE001
                # 任何异常都不得杀死 watchdog 分发线程，否则监听整体静默失效（Sol P1）
                logger.exception("watchdog event processing failed: %s %s", kind, rel)
            return
        try:
            process_event(self.cfg, self.conn, kind, rel)
        except Exception:  # noqa: BLE001
            logger.exception("watchdog event processing failed: %s %s", kind, rel)

    def on_created(self, event):
        if not event.is_directory:
            self._emit("created", _rel_to_vault(self.cfg, event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self._emit("modified", _rel_to_vault(self.cfg, event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        dest = _rel_to_vault(self.cfg, event.dest_path)
        src = _rel_to_vault(self.cfg, event.src_path)
        self._emit("moved_in", dest)
        self._emit("moved_out", src)

    def on_deleted(self, event):
        if not event.is_directory:
            self._emit("deleted", _rel_to_vault(self.cfg, event.src_path))


def matches_exclude(cfg: AppConfig, rel: str) -> bool:
    from ..security.path_guard import matches_scan_exclude

    return matches_scan_exclude(cfg.vault_root, rel, cfg.scan_exclude)


def _rel_to_vault(cfg: AppConfig, src_path: str) -> str:
    p = _P(src_path)
    try:
        return str(p.relative_to(cfg.vault_root))
    except ValueError:
        # vault 外路径不得回退成 basename——会被 resolve_in_vault 误解析到
        # vault 根下同名文件，造成错误索引/删除无关笔记（Sol P2）
        logger.warning("watchdog event outside vault ignored: %s", src_path)
        return ""


def start_watcher(cfg: AppConfig, conn, coordinator: ScanCoordinator | None = None):
    handler = VaultEventHandler(cfg, conn, cfg.debounce_ms, coordinator)
    observer = Observer()
    observer.schedule(handler, str(cfg.vault_root), recursive=True)
    observer.start()
    return observer
