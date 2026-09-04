"""
Unified IM Hub - Domain Models & Canonical Contracts
Conforms strictly to docs/03-im-integration-v0.2.7.md
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Literal, Optional


# -----------------------------------------------------------------------------
# Capability & Adapter Contracts
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class IMCapabilities:
    canReadHistory: bool
    realtime: bool
    media: Literal["none", "placeholder", "local"]
    nativeUnread: bool
    reliableSelfIdentity: bool
    mentions: bool
    replies: bool
    recallEvents: bool


@dataclass(frozen=True)
class IMCoverageGap:
    from_time: str
    through_time: str
    reason: str


@dataclass(frozen=True)
class IMCoverage:
    kind: Literal["full", "bounded", "snapshot", "realtime_only", "unknown"]
    from_time: Optional[str] = None
    through_time: Optional[str] = None
    gaps: List[IMCoverageGap] = None

    def __post_init__(self):
        if self.gaps is None:
            object.__setattr__(self, "gaps", [])


@dataclass(frozen=True)
class IMFreshness:
    stale: bool
    last_observed_at: Optional[str] = None
    source_through_at: Optional[str] = None
    lag_ms: Optional[int] = None


@dataclass(frozen=True)
class IMWatermark:
    kind: Literal["source_cursor", "event_sequence", "snapshot_version", "timestamp", "none"]
    value: Optional[str] = None
    committed_at: Optional[str] = None


@dataclass(frozen=True)
class IMSourceStatus:
    source: Literal["wechat", "wecom", "qq"]
    connectivity: Literal["live", "catching_up", "degraded", "offline", "error"]
    coverage: IMCoverage
    freshness: IMFreshness
    watermark: IMWatermark
    rebuildability: Literal["full", "snapshot_bounded", "none"]


# -----------------------------------------------------------------------------
# Normalized Entities & DTOs
# -----------------------------------------------------------------------------

@dataclass
class IMAttachment:
    type: Literal["image", "voice", "video", "file"]
    name: Optional[str] = None
    mime: Optional[str] = None
    size: Optional[int] = None
    availability: Literal["local", "placeholder", "unavailable"] = "placeholder"
    local_ref: Optional[str] = None  # e.g. "asset://<uuid>" - never real filesystem path


@dataclass
class IMMessageItem:
    id: str  # Workspace global opaque ID
    ingest_seq: int  # Monotonic sequence in IM Journal
    source: Literal["wechat", "wecom", "qq"]
    account_id: str
    channel_id: str
    source_id_quality: Literal["native", "synthetic"]
    sender_id: Optional[str]
    sender_name: str
    sender_role: Optional[str]
    is_self: Optional[bool]  # None if indeterminate (e.g. WeCom)
    text: str
    message_type: Literal["text", "image", "voice", "video", "file", "link", "notice", "mixed", "unknown"]
    mentions: List[Dict[str, Any]]
    reply_to: Optional[str]  # MUST be source-side locator or None; NEVER workspace ID (P1-IM-6-R4)
    attachments: List[IMAttachment]
    occurred_at: str  # Original ISO
    occurred_at_epoch_ms: int  # Ordering key
    observed_at: str
    provenance: Dict[str, Any]
    focus_tags: List[str]
    focus_reasons: List[str]
    source_message_id: Optional[str] = None


@dataclass
class IMChannelSummary:
    id: str  # "wechat:xxx", "wecom:yyy", "qq:zzz"
    platform: Literal["wechat", "wecom", "qq"]
    account_id: str
    channel_type: Literal["direct", "group", "notice"]
    name: string
    avatar_availability: Literal["local", "placeholder", "unavailable"]
    avatar_local_ref: Optional[str]
    last_message: str
    last_time: str
    local_unseen_count: int
    is_focus: boolean
    native_unread_count: Optional[int] = None


# -----------------------------------------------------------------------------
# Canonical Facts & Digest Computation (P1-IM-6-R2 & P1-IM-6-R4)
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalMentionFact:
    id: Optional[str]
    name: Optional[str]
    is_self: Optional[bool]  # Normalized: None if not explicitly boolean
    is_all: Optional[bool]   # Normalized: None if not explicitly boolean


@dataclass(frozen=True)
class CanonicalAttachmentFact:
    type: Literal["image", "voice", "video", "file"]
    name: Optional[str]
    mime: Optional[str]
    size: Optional[int]


@dataclass(frozen=True)
class CanonicalIMPayloadV1:
    source: Literal["wechat", "wecom", "qq"]
    account_id: str
    channel_id: str
    source_message_id: Optional[str]
    sender_id: Optional[str]
    sender_name: str
    is_self: Optional[bool]
    reply_to: Optional[str]
    text: str
    message_type: Literal["text", "image", "voice", "video", "file", "link", "notice", "mixed", "unknown"]
    mentions: List[CanonicalMentionFact]
    attachments: List[CanonicalAttachmentFact]
    occurred_at_epoch_ms: int


def build_canonical_payload(msg: IMMessageItem) -> CanonicalIMPayloadV1:
    """
    Constructs the strictly single-valued canonical payload from an IMMessageItem.
    Excludes all workspace-local and derived fields (id, ingest_seq, observed_at,
    provenance, focus_tags, focus_reasons, local_ref, availability).
    """
    # Normalize mentions
    norm_mentions: List[CanonicalMentionFact] = []
    for m in (msg.mentions or []):
        raw_is_self = m.get("is_self")
        norm_is_self = raw_is_self if isinstance(raw_is_self, bool) else None
        raw_is_all = m.get("is_all")
        norm_is_all = raw_is_all if isinstance(raw_is_all, bool) else None
        norm_mentions.append(
            CanonicalMentionFact(
                id=m.get("id") or None,
                name=m.get("name") or None,
                is_self=norm_is_self,
                is_all=norm_is_all,
            )
        )

    # Normalize attachments (exclude local_ref and availability)
    norm_attachments: List[CanonicalAttachmentFact] = []
    for att in (msg.attachments or []):
        att_type = att.type if isinstance(att, IMAttachment) else att.get("type", "file")
        att_name = (att.name if isinstance(att, IMAttachment) else att.get("name")) or None
        att_mime = (att.mime if isinstance(att, IMAttachment) else att.get("mime")) or None
        att_size = att.size if isinstance(att, IMAttachment) else att.get("size")
        norm_attachments.append(
            CanonicalAttachmentFact(
                type=att_type,
                name=att_name,
                mime=att_mime,
                size=att_size if isinstance(att_size, int) else None,
            )
        )

    return CanonicalIMPayloadV1(
        source=msg.source,
        account_id=msg.account_id,
        channel_id=msg.channel_id,
        source_message_id=msg.source_message_id or None,
        sender_id=msg.sender_id or None,
        sender_name=msg.sender_name,
        is_self=msg.is_self if isinstance(msg.is_self, bool) else None,
        reply_to=msg.reply_to or None,
        text=msg.text,
        message_type=msg.message_type,
        mentions=norm_mentions,
        attachments=norm_attachments,
        occurred_at_epoch_ms=msg.occurred_at_epoch_ms,
    )


def canonical_bytes_v1(msg: IMMessageItem) -> bytes:
    """
    Serializes the canonical payload into exact UTF-8 JSON bytes with fixed ordering.
    """
    canonical_obj = build_canonical_payload(msg)
    data = asdict(canonical_obj)
    # Fixed keys, no whitespace, separators=(',', ':')
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=False)
    return encoded.encode("utf-8")


def compute_server_digest(msg: IMMessageItem) -> str:
    """
    Computes the authoritative server SHA-256 digest for deduplication & tamper prevention.
    """
    return hashlib.sha256(canonical_bytes_v1(msg)).hexdigest()


# -----------------------------------------------------------------------------
# Ingestion Envelope & Batch (P1-IM-6-R3 & AT-5)
# -----------------------------------------------------------------------------

@dataclass
class IMIngestRecord:
    source: Literal["wechat", "wecom", "qq"]
    account_id: str
    dedupe_key: string
    dedupe_basis: Literal["native_message_id", "source_event_id", "synthetic_v1"]
    message: IMMessageItem
    provided_digest: Optional[str] = None


@dataclass
class IMIngestBatch:
    source: Literal["wechat", "wecom", "qq"]
    account_id: str
    records: List[IMIngestRecord]
    new_watermark: Optional[IMWatermark] = None


@dataclass
class IMCommitReceipt:
    source: Literal["wechat", "wecom", "qq"]
    account_id: str
    inserted_count: int
    skipped_count: int
    committed_seq_head: int
    watermark_advanced: bool
