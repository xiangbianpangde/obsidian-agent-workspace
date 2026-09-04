"""
Unified IM Hub - Zhin QQ Adapter (Push-only Driver)
Conforms strictly to docs/03-im-integration-v0.2.7.md
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.app.im.adapters.base import IMIngestDriver, IMIngestSink
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


class ZhinQQAdapter(IMIngestDriver):
    """
    Push-only QQ Adapter backed by Zhin.js Observer.
    canReadHistory = False (never implements IMSourceReader, satisfies AT-1).
    coverage = realtime_only, rebuildability = none.
    """

    def __init__(self, account_id: str = "qq_default"):
        self._account_id = account_id
        self._sink: Optional[IMIngestSink] = None
        self._running = False
        self._last_observed_at: Optional[str] = None

    @property
    def source(self) -> str:
        return "qq"

    @property
    def capabilities(self) -> IMCapabilities:
        return IMCapabilities(
            canReadHistory=False,
            realtime=True,
            media="placeholder",
            nativeUnread=False,
            reliableSelfIdentity=True,
            mentions=True,
            replies=True,
            recallEvents=True,
        )

    async def get_status(self) -> IMSourceStatus:
        return IMSourceStatus(
            source="qq",
            connectivity="live" if self._running else "offline",
            coverage=IMCoverage(
                kind="realtime_only",
                gaps=[]
            ),
            freshness=IMFreshness(
                stale=False,
                last_observed_at=self._last_observed_at
            ),
            watermark=IMWatermark(
                kind="event_sequence",
                value=self._last_observed_at,
                committed_at=self._last_observed_at
            ),
            rebuildability="none"
        )

    async def start(self, sink: IMIngestSink) -> None:
        self._sink = sink
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._sink = None

    async def ingest_inbound_event(self, event_data: Dict[str, Any]) -> Any:
        """
        Receives an authenticated one-way push from Zhin.js observer.
        Converts to IMIngestRecord with dedupe_basis = 'source_event_id'.
        """
        if not self._sink:
            raise RuntimeError("QQ IngestDriver is not started")

        event_id = event_data.get("event_id")
        if not event_id:
            raise ValueError("Missing event_id in QQ Ingress event")

        account_id = event_data.get("account_id", self._account_id)
        occurred_at = event_data.get("occurred_at", datetime.now(timezone.utc).isoformat())
        occurred_epoch = event_data.get("occurred_at_epoch_ms", int(datetime.now(timezone.utc).timestamp() * 1000))
        payload = event_data.get("payload", {})

        group_id = payload.get("group_id")
        channel_id = f"qq:group_{group_id}" if group_id else f"qq:direct_{payload.get('sender_id')}"
        channel_name = payload.get("group_name") or f"QQ好友 ({payload.get('sender_name', '未知')})"
        channel_type = "group" if group_id else "direct"

        text = payload.get("text", "")
        msg_type = payload.get("message_type", "text")
        mentions = payload.get("mentions", [])
        reply_to = payload.get("reply_to")  # MUST be source-side locator or None

        tags, reasons = evaluate_focus_rules(
            channel_name=channel_name,
            channel_type=channel_type,
            is_focus=("班" in channel_name or "课程" in channel_name),
            text=text,
            message_type=msg_type,
            mentions=mentions
        )

        msg_item = IMMessageItem(
            id=f"qq_msg_{event_id}",
            ingest_seq=0,  # Assigned on commit
            source="qq",
            account_id=account_id,
            channel_id=channel_id,
            source_id_quality="native",
            source_message_id=event_id,
            sender_id=payload.get("sender_id"),
            sender_name=payload.get("sender_name", "QQ用户"),
            sender_role=payload.get("sender_role"),
            is_self=payload.get("is_self", False),
            reply_to=reply_to,
            text=text,
            message_type=msg_type,
            mentions=mentions,
            attachments=[],
            occurred_at=occurred_at,
            occurred_at_epoch_ms=occurred_epoch,
            observed_at=datetime.now(timezone.utc).isoformat(),
            provenance={"mode": "webhook", "event_id": event_id},
            focus_tags=tags,
            focus_reasons=reasons
        )

        record = IMIngestRecord(
            source="qq",
            account_id=account_id,
            dedupe_key=event_id,
            dedupe_basis="source_event_id",
            message=msg_item,
            provided_digest=event_data.get("payload_digest")
        )

        batch = IMIngestBatch(
            source="qq",
            account_id=account_id,
            records=[record]
        )

        receipt = await self._sink.commit(batch)
        self._last_observed_at = msg_item.observed_at
        return receipt
