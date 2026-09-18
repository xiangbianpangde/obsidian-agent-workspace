"""Paper Workbench domain model.

Frozen by ADR-006 (identity), ADR-007 (authority split) and ADR-008 (annotations).

Key invariants this module encodes:

* ``Paper`` is the single first-class aggregate. Everything else hangs off it.
* ``Paper`` 1:N ``PaperSource`` — a paper may legally own several PDFs and
  several translation variants. There is deliberately **no** scalar
  ``pdfPath`` / ``translationPath`` field.
* Identity is a random UUID stored in the in-folder manifest. It is never
  derived from a path, a title or a content hash.
* Reading status lives in SQLite and is **not** mirrored into the Vault
  (ADR-007). There is no ``status`` field on the manifest model.
* Discovery is read-only: a scanned candidate carries no manifest until the
  paper is adopted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    "PaperStatus",
    "SourceRole",
    "MediaKind",
    "BindingState",
    "BindingOrigin",
    "AnnotationKind",
    "new_paper_id",
    "new_source_id",
    "new_note_id",
    "new_annotation_id",
    "utc_now",
    "PaperSource",
    "PaperNote",
    "Paper",
    "WorkspaceState",
]


def _uuid4() -> str:
    return str(uuid.uuid4())


def new_paper_id() -> str:
    """Random stable identity. Never derived from path, title or hash (ADR-006)."""
    return f"pw_{_uuid4()}"


def new_source_id() -> str:
    return f"src_{_uuid4()}"


def new_note_id() -> str:
    return f"note_{_uuid4()}"


def new_annotation_id() -> str:
    return f"ann_{_uuid4()}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperStatus(str, Enum):
    """Reading status. Authoritative in SQLite, never written to the Vault."""

    UNREAD = "UNREAD"
    READING = "READING"
    COMPLETED = "COMPLETED"

    @classmethod
    def parse(cls, value: Any) -> "PaperStatus":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(f"invalid paper status: {value!r}") from exc


class SourceRole(str, Enum):
    """Explicit source role. Guide / full / extraction must not collapse."""

    ORIGINAL_PDF = "ORIGINAL_PDF"
    SUPPLEMENTAL_PDF = "SUPPLEMENTAL_PDF"
    TRANSLATION_FULL = "TRANSLATION_FULL"
    TRANSLATION_GUIDE = "TRANSLATION_GUIDE"
    EXTRACTED_MARKDOWN = "EXTRACTED_MARKDOWN"
    OTHER_MARKDOWN = "OTHER_MARKDOWN"

    @property
    def media_kind(self) -> "MediaKind":
        return (
            MediaKind.PDF
            if self in (SourceRole.ORIGINAL_PDF, SourceRole.SUPPLEMENTAL_PDF)
            else MediaKind.MARKDOWN
        )

    @property
    def is_translation(self) -> bool:
        return self in (SourceRole.TRANSLATION_FULL, SourceRole.TRANSLATION_GUIDE)

    #: Display preference when several translation sources exist (ADR-006).
    @property
    def display_rank(self) -> int:
        return {
            SourceRole.TRANSLATION_FULL: 0,
            SourceRole.TRANSLATION_GUIDE: 1,
            SourceRole.EXTRACTED_MARKDOWN: 2,
            SourceRole.OTHER_MARKDOWN: 3,
            SourceRole.ORIGINAL_PDF: 4,
            SourceRole.SUPPLEMENTAL_PDF: 5,
        }[self]


class MediaKind(str, Enum):
    PDF = "PDF"
    MARKDOWN = "MARKDOWN"


class BindingState(str, Enum):
    """How confident the system is about this paper's source bindings."""

    #: Scanned candidate, nothing written to the Vault yet.
    DISCOVERED = "DISCOVERED"
    #: Manifest exists; identity is anchored.
    ADOPTED = "ADOPTED"
    #: Only a PDF was found; still a legal paper (ADR-006).
    PDF_ONLY = "PDF_ONLY"
    #: All bindings resolved cleanly.
    RESOLVED = "RESOLVED"
    #: Several plausible candidates; user must choose. Never auto-resolved.
    AMBIGUOUS = "AMBIGUOUS"
    #: A manifest exists but a referenced file is missing.
    DEGRADED = "DEGRADED"
    #: The same paper_id was found in two folders. Fail closed, never merge.
    DUPLICATE_ID_CONFLICT = "DUPLICATE_ID_CONFLICT"
    #: Soft-removed (``inactive_at``). Physical deletion is forbidden.
    INACTIVE = "INACTIVE"


class BindingOrigin(str, Enum):
    MANIFEST = "MANIFEST"
    STRICT_RULE = "STRICT_RULE"
    MANUAL = "MANUAL"
    DISCOVERY = "DISCOVERY"


class AnnotationKind(str, Enum):
    HIGHLIGHT = "HIGHLIGHT"
    COMMENT = "COMMENT"
    THOUGHT = "THOUGHT"
    INNOVATION = "INNOVATION"
    QUESTION = "QUESTION"
    CONCLUSION = "CONCLUSION"


@dataclass
class PaperSource:
    """One readable artifact belonging to a paper. Paths are folder-relative."""

    source_id: str
    paper_id: str
    role: SourceRole
    rel_path: str
    #: NFC-normalised comparison key. macOS stores NFD; the exact path is
    #: preserved in ``rel_path`` so writes always address the real file.
    rel_path_key_nfc: str = ""
    is_primary: bool = False
    binding_origin: BindingOrigin = BindingOrigin.STRICT_RULE
    binding_confidence: Optional[float] = None
    size_bytes: Optional[int] = None
    mtime_ns: Optional[int] = None
    sha256: Optional[str] = None
    source_version: int = 1
    mime_type: str = ""
    language: Optional[str] = None
    page_count: Optional[int] = None
    active: bool = True
    is_candidate: bool = False
    missing_since: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def media_kind(self) -> MediaKind:
        return self.role.media_kind

    def to_row(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "paper_id": self.paper_id,
            "role": self.role.value,
            "media_kind": self.media_kind.value,
            "rel_path": self.rel_path,
            "rel_path_key_nfc": self.rel_path_key_nfc or self.rel_path,
            "is_primary": int(self.is_primary),
            "binding_origin": self.binding_origin.value,
            "binding_confidence": self.binding_confidence,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "source_version": self.source_version,
            "mime_type": self.mime_type,
            "language": self.language,
            "page_count": self.page_count,
            "active": int(self.active),
            "is_candidate": int(self.is_candidate),
            "missing_since": self.missing_since,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class PaperNote:
    """A markdown note bound to a paper. Content lives in the Vault."""

    note_id: str
    paper_id: str
    rel_path: str
    content_sha256: Optional[str] = None
    note_tags: List[str] = field(default_factory=list)
    schema_version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    missing_since: Optional[str] = None
    inactive_at: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        import json

        return {
            "note_id": self.note_id,
            "paper_id": self.paper_id,
            "rel_path": self.rel_path,
            "content_sha256": self.content_sha256,
            "note_tags_json": json.dumps(self.note_tags, ensure_ascii=False),
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "missing_since": self.missing_since,
            "inactive_at": self.inactive_at,
        }


@dataclass
class Paper:
    """The aggregate root.

    Fields split by authority (ADR-007):

    * manifest-owned  : ``paper_id``, ``title_override``, ``paper_tags``,
      ``external_ids``, source bindings, note binding
    * SQLite-owned    : ``status``, ``first_opened_at``, ``last_opened_at``,
      ``completed_at``, ``binding_state``, ``category_relpath``
    """

    paper_id: str
    folder_relpath: str
    display_title: str = ""
    title_override: Optional[str] = None
    category_relpath: str = ""
    manifest_relpath: Optional[str] = None
    binding_state: BindingState = BindingState.DISCOVERED
    primary_pdf_source_id: Optional[str] = None
    primary_translation_source_id: Optional[str] = None
    note_id: Optional[str] = None
    paper_tags: List[str] = field(default_factory=list)
    external_ids: Dict[str, str] = field(default_factory=dict)
    status: PaperStatus = PaperStatus.UNREAD
    first_opened_at: Optional[str] = None
    last_opened_at: Optional[str] = None
    completed_at: Optional[str] = None
    status_changed_at: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    inactive_at: Optional[str] = None
    ambiguity_reason: Optional[str] = None
    sources: List[PaperSource] = field(default_factory=list)

    @property
    def title(self) -> str:
        """Display title precedence: override > folder name > (caller sets)."""
        return self.title_override or self.display_title or self.folder_relpath

    def to_row(self) -> Dict[str, Any]:
        import json

        return {
            "paper_id": self.paper_id,
            "folder_relpath": self.folder_relpath,
            "display_title": self.display_title,
            "title_override": self.title_override,
            "category_relpath": self.category_relpath,
            "manifest_relpath": self.manifest_relpath,
            "binding_state": self.binding_state.value,
            "primary_pdf_source_id": self.primary_pdf_source_id,
            "primary_translation_source_id": self.primary_translation_source_id,
            "note_id": self.note_id,
            "paper_tags_json": json.dumps(self.paper_tags, ensure_ascii=False),
            "external_ids_json": json.dumps(self.external_ids, ensure_ascii=False),
            "status": self.status.value,
            "first_opened_at": self.first_opened_at,
            "last_opened_at": self.last_opened_at,
            "completed_at": self.completed_at,
            "status_changed_at": self.status_changed_at,
            "ambiguity_reason": self.ambiguity_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "inactive_at": self.inactive_at,
        }


@dataclass
class WorkspaceState:
    """Reading position. SQLite-owned, LOCAL_ONLY (ADR-007).

    Deliberately stores ratios rather than raw pixel scroll offsets, so the
    position survives window resizing, zoom changes and DPI differences.
    """

    paper_id: str
    active_pdf_source_id: Optional[str] = None
    active_markdown_source_id: Optional[str] = None
    active_pane: str = "PDF"
    source_positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    note_id: Optional[str] = None
    note_cursor_start: Optional[int] = None
    note_cursor_end: Optional[int] = None
    note_content_sha256: Optional[str] = None
    last_opened_at: Optional[str] = None
    updated_at: str = field(default_factory=utc_now)
    state_version: int = 1

    def to_row(self) -> Dict[str, Any]:
        import json

        return {
            "paper_id": self.paper_id,
            "active_pdf_source_id": self.active_pdf_source_id,
            "active_markdown_source_id": self.active_markdown_source_id,
            "active_pane": self.active_pane,
            "source_positions_json": json.dumps(self.source_positions, ensure_ascii=False),
            "note_id": self.note_id,
            "note_cursor_start": self.note_cursor_start,
            "note_cursor_end": self.note_cursor_end,
            "note_content_sha256": self.note_content_sha256,
            "last_opened_at": self.last_opened_at,
            "updated_at": self.updated_at,
            "state_version": self.state_version,
        }
