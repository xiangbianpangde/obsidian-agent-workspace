"""
Unified IM Hub - Ingestion Coordinator & Reliable SSE Bus
Conforms strictly to docs/03-im-integration-v0.2.7.md
Implements Ring Buffer replay, dual-cursor resolution, Resync Fence,
and atomic commit sink.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Deque, Dict, List, Optional, Set

from backend.app.im.adapters.base import IMIngestDriver, IMIngestSink, IMSourceAdapter, IMSourceReader
from backend.app.im.adapters.qq import QQSnapshotAdapter
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


REPLAY_LIMIT = 200


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
        self._started = False

        # Initialize adapters and bind default sink
        wx_account = os.environ.get("WECHAT_ACCOUNT_ID", "wxid_hxwpag2k3qi122")
        wx_base = os.environ.get("WX_CLI_BASE_URL", "http://127.0.0.1:9100")
        wecom_account = os.environ.get("WECOM_ACCOUNT_ID", "wecom_primary")
        qq_account = os.environ.get("QQ_ACCOUNT_ID", "qq_primary")
        qq_snapshot_root = os.environ.get("QQ_SNAPSHOT_ROOT")

        self.wechat_adapter = WxCliAdapter(account_id=wx_account, base_url=wx_base)
        self.wecom_adapter = WeComSnapshotAdapter(account_id=wecom_account)
        self.qq_adapter = QQSnapshotAdapter(account_id=qq_account, snapshot_root=qq_snapshot_root)

        self.wechat_adapter._sink = self
        self.wecom_adapter._sink = self
        self.qq_adapter._sink = self

        self._adapters: Dict[str, IMSourceAdapter] = {
            "wechat": self.wechat_adapter,
            "wecom": self.wecom_adapter,
            "qq": self.qq_adapter,
        }

    async def start(self) -> None:
        """Start all background ingest drivers exactly once."""
        if self._started:
            return
        self._started = True
        for adapter in self._adapters.values():
            if isinstance(adapter, IMIngestDriver):
                await adapter.start(self)

    async def ensure_started(self) -> None:
        """Alias for start(), used for lazy boot on first API access."""
        await self.start()

    async def stop(self) -> None:
        """Stop all background ingest drivers."""
        if not self._started:
            return
        for adapter in self._adapters.values():
            if isinstance(adapter, IMIngestDriver):
                await adapter.stop()
        self._started = False

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
        # WeCom backfill can be 20k records; SQLite commit must not block the event loop (Sol P1)
        receipt = await asyncio.to_thread(self.journal.commit_batch, batch)

        if receipt.inserted_count > 0:
            # Query the newly inserted messages
            # For simplicity, query the latest batch items from journal
            cur_head = receipt.committed_seq_head
            start_seq = max(0, cur_head - receipt.inserted_count)
            new_msgs = self.journal.query_replay_events(after_seq=start_seq, limit=receipt.inserted_count)

            async with self._lock:
                for msg in new_msgs:
                    self._ring.append(msg)
                    # Broadcast to SSE subscribers; drop slow consumers instead of
                    # silently skipping (they will reconnect via Last-Event-ID resync)
                    for q in list(self._subscribers):
                        try:
                            q.put_nowait(msg)
                        except asyncio.QueueFull:
                            # Slow consumer: drop its backlog, remove it from the
                            # bus and push a sentinel so its stream emits
                            # resync_required and the client resumes via
                            # Last-Event-ID instead of silently losing events.
                            self._subscribers.discard(q)
                            try:
                                while True:
                                    q.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            q.put_nowait(None)

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

        # 5. Register the live queue BEFORE journal replay so no committed
        # message can fall into the replay/subscribe gap (Sol P1).
        # Overlap between replay and live broadcast is removed by seq dedupe.
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.add(q)
        try:
            last_seq = effective_after_seq
            if effective_after_seq < head_seq:
                missed = self.journal.query_replay_events(
                    after_seq=effective_after_seq, limit=REPLAY_LIMIT + 1
                )
                if len(missed) > REPLAY_LIMIT:
                    # Bounded replay would silently truncate (Sol P1): hand the
                    # client to the exhaustive snapshot resync path instead.
                    resync_data = json.dumps({"snapshot_head_seq": missed[-1].ingest_seq})
                    yield f"event: resync_required\ndata: {resync_data}\n\n"
                    return
                for m in missed:
                    last_seq = m.ingest_seq
                    yield self._format_sse_message(m)

            # 6. Stream live events; skip anything already covered by replay
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if msg is None:
                    # Evicted slow consumer: force deterministic resync
                    resync_data = json.dumps({"snapshot_head_seq": self.journal.get_current_head_seq()})
                    yield f"event: resync_required\ndata: {resync_data}\n\n"
                    return
                if msg.ingest_seq <= last_seq:
                    continue
                last_seq = msg.ingest_seq
                yield self._format_sse_message(msg)
        finally:
            self._subscribers.discard(q)

    def _format_sse_message(self, msg: IMMessageItem) -> str:
        """Formats message as SSE packet with id: <ingest_seq> (P1-IM-7)."""
        data = json.dumps(self._msg_to_dict(msg), ensure_ascii=False)
        return f"id: {msg.ingest_seq}\nevent: message\ndata: {data}\n\n"

    def _msg_to_dict(self, msg: IMMessageItem) -> Dict[str, Any]:
        d = asdict(msg)
        return d
