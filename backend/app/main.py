"""FastAPI app: M2 API 契约（v0.2 §6）。模板端点（templates/preview/create-with-template）由 M4 提供。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import files as files_api
from .api import tags as tags_api
from .config import load_config
from .database import sqlite

cfg = None
conn = None
coordinator = None
observer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cfg, conn, coordinator, observer
    cfg = load_config()
    conn = sqlite.connect(cfg.database_path)
    files_api.init_api(cfg, conn)
    tags_api.init_api(conn)
    if cfg.watchdog_enabled:
        from .watch.watcher import ScanCoordinator, start_watcher

        coordinator = ScanCoordinator()
        observer = start_watcher(cfg, conn, coordinator)
        yield
        observer.stop()
        observer.join()
    else:
        yield
    conn.close()


app = FastAPI(title="Obsidian Agent Workspace", version="0.2.0-m2", lifespan=lifespan)
app.include_router(files_api.router, prefix="/api", tags=["files"])
app.include_router(tags_api.router, prefix="/api", tags=["tags"])


@app.get("/api/health")
def health():
    s = sqlite.stats(conn)
    return {"ok": True, "files": s["files"], "tags": s["tags"], "watchdog": observer is not None}
