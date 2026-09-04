"""
Unified IM Hub - FastAPI Router & Internal Ingress
Conforms strictly to docs/03-im-integration-v0.2.7.md
Implements Cache-Control: no-store, loopback secret auth, and SSE endpoint.
"""

import hmac
import os
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from backend.app.im.coordinator import IngestionCoordinator
from backend.app.im.journal import IdentityConflictError, IMJournal, InvalidIngestEnvelopeError

router = APIRouter(tags=["IM Hub"])

# Default loopback shared secret for Zhin ingress
IM_INGEST_SECRET = os.environ.get("IM_INGEST_SECRET", "workspace_im_secret_token_default")

_global_journal: Optional[IMJournal] = None
_global_coordinator: Optional[IngestionCoordinator] = None


def get_im_journal() -> IMJournal:
    global _global_journal
    if _global_journal is None:
        _global_journal = IMJournal()
    return _global_journal


def get_im_coordinator() -> IngestionCoordinator:
    global _global_coordinator
    if _global_coordinator is None:
        j = get_im_journal()
        _global_coordinator = IngestionCoordinator(j)
    return _global_coordinator


def apply_no_store(response: Response) -> None:
    """Security invariant: Cache-Control: no-store for all personal IM data."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"


# -----------------------------------------------------------------------------
# Public Query Endpoints
# -----------------------------------------------------------------------------

@router.get("/api/im/status")
async def get_im_status(response: Response) -> Dict[str, Any]:
    apply_no_store(response)
    coordinator = get_im_coordinator()
    statuses = await coordinator.get_all_statuses()
    return {
        "status": "ok",
        "sources": statuses,
        "head_seq": coordinator.journal.get_current_head_seq()
    }


@router.get("/api/im/overview")
async def get_im_overview(response: Response) -> Dict[str, Any]:
    apply_no_store(response)
    journal = get_im_journal()
    channels = journal.list_channels()

    total_unseen = sum(c.local_unseen_count for c in channels)
    platform_counts = {"wechat": 0, "wecom": 0, "qq": 0}
    for c in channels:
        if c.platform in platform_counts:
            platform_counts[c.platform] += 1

    return {
        "channel_count": len(channels),
        "total_unseen": total_unseen,
        "platforms": platform_counts,
        "head_seq": journal.get_current_head_seq()
    }


@router.get("/api/im/channels")
async def list_im_channels(
    response: Response,
    platform: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    focus_only: bool = Query(False)
) -> Dict[str, Any]:
    apply_no_store(response)
    journal = get_im_journal()
    channels = journal.list_channels(platform=platform, channel_type=type, focus_only=focus_only)

    data = [
        {
            "id": c.id,
            "platform": c.platform,
            "account_id": c.account_id,
            "channel_type": c.channel_type,
            "name": c.name,
            "avatar": {
                "availability": c.avatar_availability,
                "local_ref": c.avatar_local_ref
            },
            "last_message": c.last_message,
            "last_time": c.last_time,
            "local_unseen_count": c.local_unseen_count,
            "is_focus": c.is_focus,
            "native_unread_count": c.native_unread_count
        }
        for c in channels
    ]
    return {"channels": data}


@router.get("/api/im/timeline")
async def get_im_timeline(
    response: Response,
    platform: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    snapshot_seq: Optional[int] = Query(None),
    focus_only: bool = Query(False)
) -> Dict[str, Any]:
    apply_no_store(response)
    journal = get_im_journal()
    items, next_cursor = journal.query_timeline(
        platform=platform,
        limit=limit,
        cursor=cursor,
        snapshot_seq=snapshot_seq,
        focus_only=focus_only
    )

    return {
        "items": [asdict_message(m) for m in items],
        "next_cursor": next_cursor,
        "snapshot_head_seq": snapshot_seq or journal.get_current_head_seq()
    }


@router.get("/api/im/snapshot")
async def get_im_snapshot_page(
    response: Response,
    snapshot_head_seq: int = Query(..., ge=1),
    cursor: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100)
) -> Dict[str, Any]:
    """
    Deterministic pagination for Resync Snapshot Exhaustion (P1-IM-7-R1 & AT-8):
    Returns items <= snapshot_head_seq. next_cursor is null when no more remain.
    """
    apply_no_store(response)
    journal = get_im_journal()
    items, next_cursor = journal.query_snapshot_page(
        snapshot_head_seq=snapshot_head_seq,
        cursor=cursor,
        limit=limit
    )

    return {
        "items": [asdict_message(m) for m in items],
        "next_cursor": next_cursor,
        "snapshot_head_seq": snapshot_head_seq
    }


@router.post("/api/im/channel/{channel_id:path}/seen")
async def mark_channel_seen(channel_id: str, response: Response) -> Dict[str, Any]:
    apply_no_store(response)
    journal = get_im_journal()
    journal.mark_channel_seen(channel_id)
    return {"status": "ok", "channel_id": channel_id}


@router.get("/api/im/events")
async def get_im_events(
    request: Request,
    after_seq: Optional[int] = Query(None),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID")
) -> StreamingResponse:
    """
    Unified SSE Event Bus with dual-cursor precedence and Resync Fence (P1-IM-7).
    """
    coordinator = get_im_coordinator()
    gen = coordinator.subscribe_events(
        query_after_seq=after_seq,
        header_last_event_id=last_event_id
    )

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Connection": "keep-alive",
        "Content-Type": "text/event-stream"
    }
    return StreamingResponse(gen, media_type="text/event-stream", headers=headers)


# -----------------------------------------------------------------------------
# Internal Ingress for Zhin QQ Push
# -----------------------------------------------------------------------------

@router.post("/internal/im/ingest/zhin")
async def ingest_from_zhin(
    request: Request,
    x_im_secret: Optional[str] = Header(None, alias="X-IM-Secret")
) -> Dict[str, Any]:
    """
    Internal one-way push ingress for Zhin.js.
    Bound strictly to loopback (127.0.0.1 / ::1) and validated with shared secret.
    """
    # 1. Loopback defense
    client_host = request.client.host if request.client else "unknown"
    if client_host not in ["127.0.0.1", "::1", "localhost", "testclient"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Loopback only")

    # 2. Secret authentication
    expected_secret = os.environ.get("IM_INGEST_SECRET", IM_INGEST_SECRET)
    if not x_im_secret or not hmac.compare_digest(x_im_secret, expected_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid X-IM-Secret header")

    # 3. Parse JSON body (Limit < 5MB)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    coordinator = get_im_coordinator()
    qq_adapter = coordinator.qq_adapter

    try:
        receipt = await qq_adapter.ingest_inbound_event(body)
        return {
            "status": "ok",
            "receipt": {
                "source": receipt.source,
                "account_id": receipt.account_id,
                "inserted_count": receipt.inserted_count,
                "skipped_count": receipt.skipped_count,
                "head_seq": receipt.committed_seq_head
            }
        }
    except InvalidIngestEnvelopeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except IdentityConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


def asdict_message(m: Any) -> Dict[str, Any]:
    """Safe serialization of IMMessageItem without leaking filesystem paths."""
    from dataclasses import asdict
    d = asdict(m)
    # Ensure attachments do not leak local paths
    if "attachments" in d:
        for att in d["attachments"]:
            if att.get("local_ref") and not att["local_ref"].startswith("asset://"):
                att["local_ref"] = "asset://redacted"
    return d
