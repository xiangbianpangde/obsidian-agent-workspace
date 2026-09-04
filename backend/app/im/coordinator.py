"""
Unified IM Hub - Ingestion Coordinator & Reliable SSE Bus
Conforms strictly to docs/03-im-integration-v0.2.7.md
Implements Ring Buffer replay, dual-cursor resolution, Resync Fence,
and atomic commit sink.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Deque, Dict, List, Optional, Set

from backend.app.im.adapters.base import IMIngestDriver, IMIngestSink, IMSourceAdapter, IMSourceReader
from backend.app.im.adapters.qq import ZhinQQAdapter
from backend.app.im.adapters.wechat import WxCliAdapter
from backend.app.im.adapters.wecom import WeComSnapshotAdapter
from backend.app.im.journal import IMJournal
from backend.app.im.models import (
    IMAttachment,
    IMCommitReceipt,
    IMIngestBatch,
    IMMessageItem,
    IMSourceStatus,
)


class InvalidCursorError(ValueError):
    """Raised when after_seq > current head_seq (P2-2)."""
    pass


class IngestionCoordinator(IMIngestSink):
    """
    Coordinates multi-source ingestion into IM Journal.
    Serves as the central SSE event hub with ring buffer replay and Resync Fence.
    """

    def __init__(self, journal: IMJournal, ring_buffer_capacity: int = 100):
        self.journal = journal
        self.capacity = ring_buffer_capacity
        self._ring: Deque[IMMessageItem] = deque(maxlen=ring_buffer_capacity)
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

        # Initialize adapters and bind default sink
        self.wechat_adapter = WxCliAdapter()
        self.wecom_adapter = WeComSnapshotAdapter()
        self.qq_adapter = ZhinQQAdapter()

        self.wechat_adapter._sink = self
        self.wecom_adapter._sink = self
        self.qq_adapter._sink = self

        self._adapters: Dict[str, IMSourceAdapter] = {
            "wechat": self.wechat_adapter,
            "wecom": self.wecom_adapter,
            "qq": self.qq_adapter,
        }

    async def start(self) -> None:
        """Starts all background ingest drivers."""
        for adapter in self._adapters.values():
            if isinstance(adapter, IMIngestDriver):
                await adapter.start(self)

    async def stop(self) -> None:
        """Stops all background ingest drivers."""
        for adapter in self._adapters.values():
            if isinstance(adapter, IMIngestDriver):
                await adapter.stop()

    def get_adapter(self, source: str) -> Optional[IMSourceAdapter]:
        return self._adapters.get(source)

    async def get_all_statuses(self) -> Dict[str, Any]:
        """Collects honest status & coverage from all adapters."""
        statuses = {}
        for src, adp in self._adapters.items():
            st = await adp.get_status()
            statuses[src] = asdict(st)
        return statuses

    # -------------------------------------------------------------------------
    # IMIngestSink Implementation
    # -------------------------------------------------------------------------

    async def commit(self, batch: IMIngestBatch) -> IMCommitReceipt:
        """
        Atomically commits normalized batch to IM Journal,
        pushes committed items into ring buffer, and notifies SSE subscribers.
        """
        receipt = self.journal.commit_batch(batch)

        if receipt.inserted_count > 0:
            # Query the newly inserted messages
            # For simplicity, query the latest batch items from journal
            cur_head = receipt.committed_seq_head
            start_seq = max(0, cur_head - receipt.inserted_count)
            new_msgs = self.journal.query_replay_events(after_seq=start_seq, limit=receipt.inserted_count)

            async with self._lock:
                for msg in new_msgs:
                    self._ring.append(msg)
                    # Broadcast to SSE subscribers
                    for q in list(self._subscribers):
                        try:
                            q.put_nowait(msg)
                        except asyncio.QueueFull:
                            pass

        return receipt

    # -------------------------------------------------------------------------
    # Reliable SSE Subscription & Replay (P1-IM-7 & P2-1, P2-2)
    # -------------------------------------------------------------------------

    async def subscribe_events(
        self,
        query_after_seq: Optional[int] = None,
        header_last_event_id: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        Subscribes to SSE stream with exact dual-cursor precedence and Resync Fence.
        """
        # 1. Dual-cursor unique precedence (P1-IM-7):
        # effective_after_seq = max(query.after_seq ?? 0, header.Last_Event_ID ?? 0)
        h_id = 0
        if header_last_event_id:
            try:
                h_id = int(header_last_event_id)
            except ValueError:
                pass

        q_id = query_after_seq if query_after_seq is not None else 0
        effective_after_seq = max(q_id, h_id)

        head_seq = self.journal.get_current_head_seq()

        # 2. Future cursor defense (P2-2):
        if effective_after_seq > head_seq:
            yield f"event: error\ndata: {json.dumps({'error': '400 InvalidCursor: cursor exceeds current head'})}\n\n"
            return

        # 3. Check ring buffer bounds
        async with self._lock:
            ring_items = list(self._ring)

        ring_floor = ring_items[0].ingest_seq if ring_items else (head_seq + 1)

        # 4. If cursor is requested but fell off the ring window, emit resync_required
        if effective_after_seq > 0 and effective_after_seq < (ring_floor - 1):
            # Fell off ring buffer window! Emit resync_required with snapshot_head_seq and close
            resync_data = json.dumps({"snapshot_head_seq": head_seq})
            yield f"event: resync_required\ndata: {resync_data}\n\n"
            return

        # 5. Replay missed items from ring buffer or journal (Open interval: seq > effective_after_seq)
        if effective_after_seq < head_seq:
            missed = self.journal.query_replay_events(after_seq=effective_after_seq, limit=200)
            for m in missed:
                yield self._format_sse_message(m)

        # 6. Stream live events
        q: asyncio.Queue[IMMessageItem] = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        try:
            while True:
                # Send periodic heartbeat comment or message
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield self._format_sse_message(msg)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subscribers.discard(q)

    def _format_sse_message(self, msg: IMMessageItem) -> str:
        """Formats message as SSE packet with id: <ingest_seq> (P1-IM-7)."""
        data = json.dumps(self._msg_to_dict(msg), ensure_ascii=False)
        return f"id: {msg.ingest_seq}\nevent: message\ndata: {data}\n\n"

    def _msg_to_dict(self, msg: IMMessageItem) -> Dict[str, Any]:
        d = asdict(msg)
        return d
