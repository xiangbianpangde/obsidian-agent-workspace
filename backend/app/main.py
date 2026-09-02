"""FastAPI app: M2 API 契约（v0.2 §6）。模板端点（templates/preview/create-with-template）由 M4 提供。
P1-M2-1: API（per-request）/ watchdog / scanner 各用独立 connection。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import files as files_api
from .api import tags as tags_api
from .config import load_config
from .database import sqlite
from .state import init_state

observer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global observer
    cfg = load_config()
    init_state(cfg)
    watchdog_conn = None
    if cfg.watchdog_enabled:
        from .watch.watcher import ScanCoordinator, start_watcher

        watchdog_conn = sqlite.connect(cfg.database_path)
        coordinator = ScanCoordinator()
        observer = start_watcher(cfg, watchdog_conn, coordinator)
    yield
    if observer is not None:
        observer.stop()
        observer.join()
    if watchdog_conn is not None:
        watchdog_conn.close()


app = FastAPI(title="Obsidian Agent Workspace", version="0.2.0-m2", lifespan=lifespan)
app.include_router(files_api.router, prefix="/api", tags=["files"])
app.include_router(tags_api.router, prefix="/api", tags=["tags"])


@app.get("/api/health")
def health():
    from .deps import get_conn

    conn_gen = get_conn()
    conn = next(conn_gen)
    try:
        s = sqlite.stats(conn)
    finally:
        conn.close()
    return {"ok": True, "files": s["files"], "tags": s["tags"], "watchdog": observer is not None}
