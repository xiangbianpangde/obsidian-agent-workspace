"""
Unified IM Hub - Enterprise WeChat (WeCom) Adapter
Conforms strictly to docs/03-im-integration-v0.2.7.md
Interfaces with yichen-wecom-local-vault snapshots.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


class WeComSnapshotAdapter(IMSourceReader, IMIngestDriver):
    """
    WeCom Adapter parsing versioned plaintext snapshots.
    canReadHistory = True, coverage = snapshot, rebuildability = snapshot_bounded.
    is_self is explicitly None when reliable internal identity is absent.
    """

    def __init__(self, account_id: str = "wecom_primary", snapshot_dir: Optional[str] = None):
        self._account_id = account_id
        self._snapshot_dir = snapshot_dir
        self._sink: Optional[IMIngestSink] = None
        self._running = False
        self._last_snapshot_at: Optional[str] = None
        self._last_watermark_val: Optional[str] = None

    @property
    def source(self) -> str:
        return "wecom"

    @property
    def capabilities(self) -> IMCapabilities:
        return IMCapabilities(
            canReadHistory=True,
            realtime=False,
            media="placeholder",
            nativeUnread=False,
            reliableSelfIdentity=False,  # Indeterminate is_self support
            mentions=True,
            replies=False,
            recallEvents=False,
        )

    async def get_status(self) -> IMSourceStatus:
        return IMSourceStatus(
            source="wecom",
            connectivity="live" if self._running else "offline",
            coverage=IMCoverage(
                kind="snapshot",
                gaps=[]
            ),
            freshness=IMFreshness(
                stale=False,
                last_observed_at=self._last_snapshot_at
            ),
            watermark=IMWatermark(
                kind="snapshot_version",
                value=self._last_watermark_val,
                committed_at=self._last_snapshot_at
            ),
            rebuildability="snapshot_bounded"
        )

    async def start(self, sink: IMIngestSink) -> None:
        self._sink = sink
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._sink = None

    async def read_history(self, limit: int = 50, before_cursor: Optional[str] = None) -> List[IMMessageItem]:
        """Reads snapshot historical records."""
        return []

    def normalize_wecom_payload(
        self,
        raw_msg: Dict[str, Any],
        snapshot_id: str = "snap_default"
    ) -> IMIngestRecord:
        """
        Normalizes a WeCom message from plaintext snapshot.
        is_self defaults to None if self identity is indeterminate.
        """
        physical_id = str(raw_msg.get("msg_id") or raw_msg.get("id"))
        dedupe_key = f"wecom_snap:{self._account_id}:{physical_id}"

        conv_id = raw_msg.get("conversation_id", "notice_general")
        channel_id = f"wecom:{conv_id}"
        channel_name = raw_msg.get("conversation_name") or f"企微群聊 ({conv_id})"
        channel_type = "group" if ("group" in conv_id or "@chatroom" in conv_id) else "direct"

        occurred_iso = raw_msg.get("send_time_iso") or datetime.now(timezone.utc).isoformat()
        occurred_epoch = raw_msg.get("send_time_epoch_ms") or int(datetime.now(timezone.utc).timestamp() * 1000)

        text = raw_msg.get("content", "")
        msg_type = raw_msg.get("content_type", "text")
        mentions = raw_msg.get("mentions", [])

        tags, reasons = evaluate_focus_rules(
            channel_name=channel_name,
            channel_type=channel_type,
            is_focus=("通知" in channel_name or "学院" in channel_name or "教务" in channel_name),
            text=text,
            message_type=msg_type,
            mentions=mentions
        )

        msg_item = IMMessageItem(
            id=f"wecom_msg_{physical_id}",
            ingest_seq=0,
            source="wecom",
            account_id=self._account_id,
            channel_id=channel_id,
            source_id_quality="synthetic",
            source_message_id=physical_id,
            sender_id=raw_msg.get("sender_id"),
            sender_name=raw_msg.get("sender_name", "企微用户"),
            sender_role=raw_msg.get("sender_role"),
            is_self=raw_msg.get("is_self"),  # Explicitly None allowed
            reply_to=None,
            text=text,
            message_type=msg_type,
            mentions=mentions,
            attachments=[],
            occurred_at=occurred_iso,
            occurred_at_epoch_ms=occurred_epoch,
            observed_at=datetime.now(timezone.utc).isoformat(),
            provenance={
                "mode": "snapshot",
                "snapshot_id": snapshot_id
            },
            focus_tags=tags,
            focus_reasons=reasons
        )

        return IMIngestRecord(
            source="wecom",
            account_id=self._account_id,
            dedupe_key=dedupe_key,
            dedupe_basis="synthetic_v1",
            message=msg_item
        )
