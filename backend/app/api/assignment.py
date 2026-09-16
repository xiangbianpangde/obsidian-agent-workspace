"""FastAPI router for assignment platforms (read-only aggregation)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..assignment.http_client import AdapterError, CredentialInvalidError
from ..assignment.storage import AssignmentStorage
from ..assignment.sync import (
    PLATFORMS,
    sync_all,
    sync_platform,
    validate_and_store_credential,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assignment", tags=["assignment"])

_storage: AssignmentStorage | None = None
_storage_lock = threading.Lock()


def get_storage() -> AssignmentStorage:
    global _storage
    with _storage_lock:
        if _storage is None:
            _storage = AssignmentStorage()
        return _storage


def _apply_no_store(response: JSONResponse) -> JSONResponse:
    """Security invariant: personal credentials & tasks never cached."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


class CredentialIn(BaseModel):
    credential: str = Field(
        ..., min_length=8, max_length=4096, description="浏览器复制的 cookie / Bearer token"
    )


# --------------------------------------------------------------------- credentials
@router.get("/credentials")
def list_credentials() -> JSONResponse:
    storage = get_storage()
    status = {s["platform"]: s for s in storage.credential_status()}
    for s in status.values():
        s["preview"] = storage.credential_preview(s["platform"])
    return _apply_no_store(JSONResponse(content={"platforms": status}))


@router.post("/credentials/{platform}")
def import_credential(platform: str, body: CredentialIn) -> JSONResponse:
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")
    storage = get_storage()
    try:
        result = validate_and_store_credential(storage, platform, body.credential.strip())
    except CredentialInvalidError as exc:
        storage.mark_validation(platform, False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AdapterError as exc:
        storage.mark_validation(platform, False)
        raise HTTPException(status_code=502, detail=f"platform unreachable: {exc}") from exc
    return _apply_no_store(JSONResponse(content=result))


@router.post("/credentials/{platform}/disable")
def disable_credential(platform: str) -> JSONResponse:
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")
    ok = get_storage().disable_credential(platform)
    if not ok:
        raise HTTPException(status_code=404, detail="credential not configured")
    return _apply_no_store(JSONResponse(content={"platform": platform, "disabled": True}))


# -------------------------------------------------------------------------- sync
@router.post("/sync/{platform}")
def sync_one(platform: str) -> JSONResponse:
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")
    result = sync_platform(get_storage(), platform)
    if result.get("error") == "throttled":
        # 风控：文档 §4.3 要求 1 次/5 分钟上限。429 让调用方拿到 retry_after
        # 而不是把节流误认为平台故障。
        return _apply_no_store(
            JSONResponse(
                content=result,
                status_code=429,
                headers={"Retry-After": str(result.get("retry_after", 0))},
            )
        )
    code = 200 if result.get("ok") else 502
    return _apply_no_store(JSONResponse(content=result, status_code=code))


@router.post("/sync-all")
def sync_everything() -> JSONResponse:
    results = sync_all(get_storage())
    throttled = [r for r in results if r.get("error") == "throttled"]
    if throttled and len(throttled) == len(results):
        retry_after = min(r.get("retry_after", 0) for r in throttled)
        return _apply_no_store(
            JSONResponse(
                content={"results": results},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        )
    any_ok = any(r.get("ok") for r in results)
    return _apply_no_store(
        JSONResponse(content={"results": results}, status_code=200 if any_ok else 502)
    )


# ------------------------------------------------------------------------- tasks
@router.get("/tasks")
def list_tasks(
    platform: str | None = Query(None),
    status: str | None = Query(None),
    include_stale: bool = Query(False),
    limit: int = Query(500, ge=1, le=2000),
) -> JSONResponse:
    if platform is not None and platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")
    if status is not None and status not in ("unsubmitted", "submitted", "graded", "unknown"):
        raise HTTPException(status_code=422, detail=f"unknown status: {status}")
    rows = get_storage().list_tasks(
        platform=platform, status=status, include_stale=include_stale, limit=limit
    )
    return _apply_no_store(JSONResponse(content={"tasks": rows, "count": len(rows)}))


@router.get("/tasks/due-soon")
def tasks_due_soon(within_hours: int = Query(48, ge=1, le=336)) -> JSONResponse:
    from ..assignment.sync import due_soon_tasks

    rows = due_soon_tasks(get_storage(), within_hours=within_hours)
    return _apply_no_store(JSONResponse(content={"tasks": rows, "count": len(rows)}))


@router.get("/status")
def assignment_status() -> JSONResponse:
    storage = get_storage()
    cred_status = {s["platform"]: s for s in storage.credential_status()}
    out: dict[str, Any] = {"platforms": {}}
    for p in PLATFORMS:
        out["platforms"][p] = {
            "credential": cred_status.get(p),
            "last_sync": storage.last_sync(p),
            "task_count": len(storage.list_tasks(platform=p, limit=2000)),
        }
    return _apply_no_store(JSONResponse(content=out))
