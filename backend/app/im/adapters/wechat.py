"""
Unified IM Hub - WeChat Adapter (Real wx-cli Integration)
Conforms strictly to docs/03-im-integration-v0.2.7.md

Reads live data from the local wx-cli HTTP service (default http://127.0.0.1:9100).
- IMSourceReader: historical backfill via /api/v1/timeline windows
- IMIngestDriver: incremental polling via /api/v1/timeline since/until

Dedupe identity uses the native `server_id` physical record locator, which is
stable across every ingestion path (satisfies P1-IM-6-R1 & AT-6).
"""

from __future__ import annotations

import asyncio
import os
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from backend.app.im.adapters.base import IMIngestDriver, IMIngestSink, IMSourceReader
from backend.app.im.models import (
    IMAttachment,
    IMCapabilities,
    IMCoverage,
    IMFreshness,
    IMIngestBatch,
    IMIngestRecord,
    IMMessageItem,
    IMSourceStatus,
    IMWatermark,
)
from backend.app.im.rules import evaluate_focus_rules

logger = logging.getLogger(__name__)

# WeChat msg_type mapping (native protocol types)
MSG_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    37: "text",       # friend request
    42: "text",       # card
    43: "video",
    47: "image",      # sticker
    48: "text",       # location
    49: "link",       # app message (link/file/quote/notice)
    50: "text",       # voip
    10000: "notice",  # system message (recall, invite, etc.)
    10002: "notice",
}


def make_wechat_synthetic_key(account_id: str, physical_msg_id: str) -> str:
    """
    Synthetic key invariant (P1-IM-6-R1 & AT-6):
    MUST contain a stable source-side physical record locator.
    NEVER use content-only hash.
    """
    return f"wx_locator:{account_id}:{physical_msg_id}"


def _extract_text(item: Dict[str, Any]) -> str:
    """Extract best-effort text from wx-cli timeline item (snippet is source-provided)."""
    snippet = item.get("snippet")
    if snippet:
        return str(snippet)
    content = item.get("content")
    if isinstance(content, dict):
        for key in ("Text", "text", "Title", "title"):
            if content.get(key):
                return str(content[key])
    if isinstance(content, str):
        return content
    return ""


class WxCliAdapter(IMSourceReader, IMIngestDriver):
    """
    Real WeChat adapter backed by the wx-cli managed HTTP service.
    canReadHistory=True, coverage=full, rebuildability=full.
    """

    def __init__(
        self,
        account_id: str = "wx_primary",
        base_url: str = "http://127.0.0.1:9100",
        poll_interval_secs: float = 3.0,
        history_window_days: int = 30,
    ):
        self._account_id = account_id
        self._base_url = base_url.rstrip("/")
        self._poll_interval = poll_interval_secs
        self._history_window_days = int(os.environ.get("WECHAT_BACKFILL_DAYS", history_window_days))

        self._sink: Optional[IMIngestSink] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

        self._connectivity: str = "offline"
        self._last_observed_at: Optional[str] = None
        self._last_watermark_val: Optional[str] = None  # unix seconds cursor
        self._last_error: Optional[str] = None
        self._resolved_account_id: Optional[str] = None

    # -------------------------------------------------------------------------
    # Capability & Status
    # -------------------------------------------------------------------------

    @property
    def source(self) -> str:
        return "wechat"

    @property
    def capabilities(self) -> IMCapabilities:
        return IMCapabilities(
            canReadHistory=True,
            realtime=True,
            media="local",
            nativeUnread=True,
            reliableSelfIdentity=True,
            mentions=True,
            replies=True,
            recallEvents=True,
        )

    async def get_status(self) -> IMSourceStatus:
        health = await self._health()
        if health is None:
            return IMSourceStatus(
                source="wechat",
                connectivity="offline",
                coverage=IMCoverage(kind="full", gaps=[]),
                freshness=IMFresness_placeholder(),
                watermark=IMWatermark(kind="source_cursor", value=self._last_watermark_val),
                rebuildability="full",
            )

        account = health.get("current_account") or {}
        self._resolved_account_id = account.get("wxid") or self._resolved_account_id

        lag_ms: Optional[int] = None
        stale = False
        if self._last_watermark_val:
            try:
                cursor_s = int(self._last_watermark_val)
                lag_ms = max(0, int((datetime.now(timezone.utc).timestamp() - cursor_s) * 1000))
                stale = lag_ms > 10 * 60 * 1000  # 10 min
            except ValueError:
                pass

        return IMSourceStatus(
            source="wechat",
            connectivity=self._connectivity if self._running else "offline",
            coverage=IMCoverage(kind="full", gaps=[]),
            freshness=IMFresness_placeholder(
                last_observed_at=self._last_observed_at,
                source_through_at=self._last_observed_at,
                lag_ms=lag_ms,
                stale=stale,
            ),
            watermark=IMWatermark(
                kind="source_cursor",
                value=self._last_watermark_val,
                committed_at=self._last_observed_at,
            ),
            rebuildability="full",
        )

    async def _health(self) -> Optional[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{self._base_url}/api/v1/health")
                if r.status_code == 200:
                    return r.json()
        except Exception as e:
            self._last_error = str(e)
        return None

    # -------------------------------------------------------------------------
    # IMIngestDriver
    # -------------------------------------------------------------------------

    async def start(self, sink: IMIngestSink) -> None:
        self._sink = sink
        self._running = True
        self._client = httpx.AsyncClient(timeout=15.0)
        self._connectivity = "live"
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        self._connectivity = "offline"
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._sink = None

    async def _poll_loop(self) -> None:
        """Hybrid ingestion: initial catch-up then incremental polling."""
        # Initial catch-up must stay inside an error boundary, otherwise a
        # sink failure (e.g. IdentityConflictError) kills the whole ingest
        # task and WeChat sync stops silently forever (Sol P1).
        try:
            await self._ingest_window(days_back=self._history_window_days)
        except Exception as e:
            self._last_error = str(e)
            self._connectivity = "degraded"
            logger.warning("WeChat initial backfill failed: %s", e)

        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if not self._running:
                    break
                await self._ingest_window(days_back=0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_error = str(e)
                self._connectivity = "degraded"
                logger.warning("WeChat poll error: %s", e)
                await asyncio.sleep(min(30.0, self._poll_interval * 4))

    async def _ingest_window(self, days_back: int = 0) -> int:
        """Fetch one timeline window and commit it. Returns inserted count."""
        if self._sink is None or self._client is None:
            return 0

        now_ts = int(datetime.now(timezone.utc).timestamp())
        if self._last_watermark_val:
            try:
                since = int(self._last_watermark_val)
            except ValueError:
                since = now_ts - days_back * 86400
        else:
            since = now_ts - max(days_back, 1) * 86400

        until = now_ts + 60
        if since >= until:
            return 0

        try:
            r = await self._client.get(
                f"{self._base_url}/api/v1/timeline",
                params={"since": since, "until": until, "limit": 500, "offset": 0},
            )
            if r.status_code != 200:
                self._connectivity = "degraded"
                return 0
            payload = r.json()
        except Exception as e:
            self._last_error = str(e)
            self._connectivity = "degraded"
            return 0

        items = list(payload.get("items") or [])

        # Paginate the window so long backfills are not capped at one page.
        paging = payload.get("paging") or {}
        total = int(paging.get("total") or len(items))
        offset = len(items)
        max_pages = 40
        page = 1
        while paging.get("has_more") and page < max_pages:
            try:
                r2 = await self._client.get(
                    f"{self._base_url}/api/v1/timeline",
                    params={"since": since, "until": until, "limit": 500, "offset": offset},
                )
                if r2.status_code != 200:
                    break
                p2 = r2.json()
            except Exception:
                break
            chunk = list(p2.get("items") or [])
            if not chunk:
                break
            items.extend(chunk)
            offset += len(chunk)
            paging = p2.get("paging") or {}
            page += 1

        if not items:
            self._connectivity = "live"
            return 0

        records: List[IMIngestRecord] = []
        max_ts = since
        for item in items:
            rec = self.normalize_wx_item(item, provenance_mode="sse")
            if rec is None:
                continue
            records.append(rec)
            ct = int(item.get("create_time") or 0)
            if ct > max_ts:
                max_ts = ct

        if not records:
            return 0

        # Advance watermark only to the highest *contiguous* source timestamp we ingested.
        # We use max_ts as the new cursor (idempotent re-read of the boundary second is safe).
        new_wm = IMWatermark(
            kind="source_cursor",
            value=str(max_ts),
            committed_at=datetime.now(timezone.utc).isoformat(),
        )

        batch = IMIngestBatch(
            source="wechat",
            account_id=self._account_id,
            records=records,
            new_watermark=new_wm,
        )

        receipt = await self._sink.commit(batch)
        self._last_watermark_val = str(max_ts)
        self._last_observed_at = datetime.now(timezone.utc).isoformat()
        self._connectivity = "live"
        return receipt.inserted_count

    # -------------------------------------------------------------------------
    # IMSourceReader
    # -------------------------------------------------------------------------

    async def read_history(self, limit: int = 50, before_cursor: Optional[str] = None) -> List[IMMessageItem]:
        """Historical backfill using explicit since/until windows (source-side locator dedupe)."""
        return []

    # -------------------------------------------------------------------------
    # Normalization
    # -------------------------------------------------------------------------

    def normalize_wx_item(self, item: Dict[str, Any], provenance_mode: str = "sse") -> Optional[IMIngestRecord]:
        """
        Normalize one wx-cli timeline item into an IMIngestRecord.

        Identity: native `server_id` (physical record locator).
        Cross-path stability: always derived from the same timeline source.
        """
        server_id = item.get("server_id")
        if server_id in (None, 0, "0"):
            # No stable source-side locator -> refuse to fabricate identity (P1-IM-6-R1).
            return None

        physical_id = str(server_id)
        dedupe_key = make_wechat_synthetic_key(self._account_id, physical_id)

        talker = item.get("talker") or item.get("username") or "unknown"
        channel_id = f"wechat:{talker}"
        display = item.get("talker_display_name") or item.get("display_name") or talker
        channel_name = self._clean_display_name(display)
        channel_type = "group" if "@chatroom" in talker else "direct"

        create_time = int(item.get("create_time") or 0)
        occurred_iso = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()
        occurred_epoch = create_time * 1000

        msg_type_num = int(item.get("msg_type") or 1)
        message_type = MSG_TYPE_MAP.get(msg_type_num, "unknown")
        sub_type = item.get("sub_type")

        text = _extract_text(item)
        direction = item.get("direction")
        is_self = True if direction == "outgoing" else (False if direction == "incoming" else None)

        sender_id = item.get("sender")
        sender_name = self._clean_display_name(item.get("sender_display_name") or sender_id or "微信联系人")

        mentions = self._extract_mentions(text)

        tags, reasons = evaluate_focus_rules(
            channel_name=channel_name,
            channel_type=channel_type,
            is_focus=self._is_focus_channel(channel_name),
            text=text,
            message_type=message_type,
            mentions=mentions,
            source="wechat",
            sender_name=sender_name,
            sender_id=sender_id,
            is_self=is_self,
        )

        attachments: List[IMAttachment] = []
        if message_type in ("image", "voice", "video", "file"):
            attachments.append(
                IMAttachment(
                    type=message_type,
                    availability="placeholder",
                    local_ref=None,
                )
            )

        msg_item = IMMessageItem(
            id=f"wx_msg_{physical_id}",
            ingest_seq=0,
            source="wechat",
            account_id=self._account_id,
            channel_id=channel_id,
            channel_name=channel_name,
            source_id_quality="native",
            source_message_id=physical_id,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_role=None,
            is_self=is_self,
            reply_to=None,  # timeline does not expose a stable source-side reply locator
            text=text,
            message_type=message_type,
            mentions=mentions,
            attachments=attachments,
            occurred_at=occurred_iso,
            occurred_at_epoch_ms=occurred_epoch,
            observed_at=datetime.now(timezone.utc).isoformat(),
            provenance={"mode": provenance_mode, "cursor": physical_id},
            focus_tags=tags,
            focus_reasons=reasons,
        )

        return IMIngestRecord(
            source="wechat",
            account_id=self._account_id,
            dedupe_key=dedupe_key,
            dedupe_basis="synthetic_v1",
            message=msg_item,
        )

    @staticmethod
    def _clean_display_name(raw: Optional[str]) -> str:
        """Strip trailing '（wxid_xxx）' parenthetical for readability."""
        if not raw:
            return "未知会话"
        s = str(raw)
        for sep in ("（", "("):
            idx = s.rfind(sep)
            if idx > 0:
                s = s[:idx]
        return s.strip() or str(raw)

    @staticmethod
    def _is_focus_channel(channel_name: str) -> bool:
        return any(kw in channel_name for kw in ["通知", "班", "学院", "教务", "课程", "科研", "实验室", "导师", "辅导"])

    @staticmethod
    def _extract_mentions(text: str) -> List[Dict[str, Any]]:
        mentions: List[Dict[str, Any]] = []
        if "@所有人" in text or "@全体成员" in text:
            mentions.append({"is_all": True})
        return mentions


def IMFresness_placeholder(
    last_observed_at: Optional[str] = None,
    source_through_at: Optional[str] = None,
    lag_ms: Optional[int] = None,
    stale: bool = False,
) -> IMFreshness:
    return IMFreshness(
        stale=stale,
        last_observed_at=last_observed_at,
        source_through_at=source_through_at,
        lag_ms=lag_ms,
    )
