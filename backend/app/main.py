"""FastAPI app: M2 API 契约（v0.2 §6）。模板端点（templates/preview/create-with-template）由 M4 提供。
P1-M2-1: API（per-request）/ watchdog / scanner 各用独立 connection。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import logging

from .api import agentsview as agentsview_api
from .api import assignment as assignment_api
from .api import files as files_api
from .api import im as im_api
from .api import schedule as schedule_api
from .api import tags as tags_api
from .api import templates as templates_api
from .paper import api as paper_api
from .paper import api_sources as paper_sources_api
from .config import PROJECT_ROOT, load_config
from .database import sqlite
from .state import get_cfg, init_state

observer = None
logger = logging.getLogger(__name__)
#: Module-level so the lock outlives the request that created it and is
#: released at process exit.
_vault_lock = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global observer, _vault_lock
    try:
        cfg = get_cfg()
    except Exception:
        cfg = load_config()
        init_state(cfg)

    # Layer 1 of the write-coordination model: refuse an obviously wrong launch.
    # A multi-worker start would give every worker its own path locks, so two
    # requests could interleave a read-modify-write on one sidecar and lose one.
    from .paper.ownership import VaultWriteLock, assert_single_worker

    assert_single_worker()

    # Layer 2: stop two separately-started processes from owning the same vault.
    # The in-process locks are layer 3 and remain the actual serialisation.
    _vault_lock = VaultWriteLock(vault_root=cfg.vault_root)
    try:
        _vault_lock.acquire()
    except Exception as exc:
        logger.error("paper vault lock not acquired: %s", exc)
        _vault_lock = None

    watchdog_conn = None
    im_coordinator = im_api.get_im_coordinator()
    try:
        await im_coordinator.start()
        if cfg.watchdog_enabled:
            from .watch.watcher import ScanCoordinator, start_watcher

            watchdog_conn = sqlite.connect(cfg.database_path)
            coordinator = ScanCoordinator()
            observer = start_watcher(cfg, watchdog_conn, coordinator)
        yield
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
            observer = None
        if watchdog_conn is not None:
            watchdog_conn.close()
        await im_coordinator.stop()
        im_coordinator.journal.close()


app = FastAPI(title="Obsidian Agent Workspace", version="0.2.0-m4", lifespan=lifespan)


@app.middleware("http")
async def im_no_store_middleware(request: Request, call_next):
    """Keep personal data out of caches, including on the error path.

    A middleware whose only job is to stamp a header after `call_next` misses
    every response that never returns from it — an unhandled exception produces
    a 500 from Starlette's own handler, which then carries no `no-store` and may
    embed private paths in its body. The header is therefore also applied in the
    exception path.
    """
    sensitive = _is_sensitive_path(request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        if sensitive:
            response = JSONResponse(
                status_code=500,
                content={"detail": "internal error"},
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )
            response.headers["Pragma"] = "no-cache"
            return response
        raise
    if sensitive:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


def _is_sensitive_path(path: str) -> bool:
    """Paths whose responses may contain personal reading or message data."""
    return path.startswith(
        (
            "/api/im",
            "/internal/im",
            "/api/schedule",
            "/api/assignment",
            "/api/paper",
        )
    )


app.include_router(files_api.router, prefix="/api", tags=["files"])
app.include_router(tags_api.router, prefix="/api", tags=["tags"])
app.include_router(templates_api.router, prefix="/api", tags=["templates"])
app.include_router(agentsview_api.router, prefix="/api/agentsview", tags=["agentsview"])
app.include_router(im_api.router)
app.include_router(schedule_api.router)
app.include_router(assignment_api.router)
app.include_router(paper_api.router)
app.include_router(paper_sources_api.router)


@app.get("/api/health")
def health():
    conn = sqlite.connect(get_cfg().database_path)
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
