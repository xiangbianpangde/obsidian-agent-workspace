"""FastAPI app: M2 API 契约（v0.2 §6）。模板端点（templates/preview/create-with-template）由 M4 提供。
P1-M2-1: API（per-request）/ watchdog / scanner 各用独立 connection。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import files as files_api
from .api import tags as tags_api
from .api import templates as templates_api
from .api import agentsview as agentsview_api
from .api import im as im_api
from .api import schedule as schedule_api
from .config import PROJECT_ROOT, load_config
from .database import sqlite
from .state import get_cfg, init_state

observer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global observer
    try:
        cfg = get_cfg()
    except Exception:
        cfg = load_config()
        init_state(cfg)
    watchdog_conn = None
    im_coordinator = im_api.get_im_coordinator()
    await im_coordinator.start()
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
    await im_coordinator.stop()


app = FastAPI(title="Obsidian Agent Workspace", version="0.2.0-m4", lifespan=lifespan)


@app.middleware("http")
async def im_no_store_middleware(request: Request, call_next):
    """Keep all personal IM & Schedule success/error responses out of browser/proxy caches."""
    response = await call_next(request)
    if (
        request.url.path.startswith("/api/im")
        or request.url.path.startswith("/internal/im")
        or request.url.path.startswith("/api/schedule")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


app.include_router(files_api.router, prefix="/api", tags=["files"])
app.include_router(tags_api.router, prefix="/api", tags=["tags"])
app.include_router(templates_api.router, prefix="/api", tags=["templates"])
app.include_router(agentsview_api.router, prefix="/api/agentsview", tags=["agentsview"])
app.include_router(im_api.router)
app.include_router(schedule_api.router)


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


dist_dir = PROJECT_ROOT / "frontend" / "dist"
if dist_dir.exists():
    app.mount("/static", StaticFiles(directory=dist_dir), name="static")


@app.get("/")
def serve_index():
    index_file = dist_dir / "index.html"
    if index_file.is_file():
        return FileResponse(
            index_file,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return {"message": "Obsidian Agent Workspace API Running"}
