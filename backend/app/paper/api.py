"""Paper Workbench API.

Serves the Paper aggregate to the frontend. Every resource is keyed by an opaque
ASCII id — never by a Vault path (ADR-006 / ADR-009), because the real vault is
full of Chinese names, emoji and full-width colons, and macOS stores them as NFD
while other tools produce NFC.

Authority split (ADR-007) is visible in the shapes here:

* reading status and workspace state are served from SQLite;
* note content is read from and written to the Vault, through
  ``VaultWriteService`` so the optimistic lock, pre-commit re-check and
  versioned backup always apply;
* the API returns the two assembled, so callers never see the seam.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..state import get_cfg
from . import ANNOTATION_STORE_FILENAME, storage as paper_storage
from .contracts import (
    ANCHOR_SCHEMA_VERSION_RESOLVABLE,
    validate_annotations,
)

#: Version stamped on newly created annotations.
ANCHOR_SCHEMA_VERSION = ANCHOR_SCHEMA_VERSION_RESOLVABLE
from .manifest import (
    DEPENDENT_OPERATIONS,
    AdoptionRequired,
    ManifestError,
    ensure_adopted,
    update_manifest,
)
from .models import (
    Paper,
    PaperNote,
    PaperSource,
    PaperStatus,
    new_annotation_id,
    new_note_id,
    utc_now,
)
from .writer import (
    AlreadyExistsError,
    ConflictError,
    PathRejected,
    VaultWriteService,
)

logger = logging.getLogger(__name__)

#: Frozen by ADR-008 alongside the sidecar schema.
ANNOTATION_KINDS = {
    "HIGHLIGHT",
    "COMMENT",
    "THOUGHT",
    "INNOVATION",
    "QUESTION",
    "CONCLUSION",
}

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _no_store(payload: Any, status_code: int = 200) -> JSONResponse:
    """Personal reading data must never be cached by browser or proxy."""
    return JSONResponse(
        content=payload,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def _storage() -> paper_storage.PaperStorage:
    return paper_storage.PaperStorage()


# --------------------------------------------------------------------- locks
#
# `_service()` used to build a fresh VaultWriteService per request, so its
# per-path locks were per-request and serialised nothing across requests. The
# service is now a module-level singleton, and annotation read-modify-write
# cycles take an additional per-paper lock, because the unit that must be
# atomic is "load sidecar -> mutate -> save", not a single file operation.

_service_singleton: Optional[VaultWriteService] = None
_service_lock = threading.Lock()
_paper_locks: Dict[str, threading.RLock] = {}
_paper_locks_guard = threading.Lock()


def _service() -> VaultWriteService:
    global _service_singleton
    cfg = get_cfg()
    with _service_lock:
        if _service_singleton is None or _service_singleton.vault_root != cfg.vault_root:
            _service_singleton = VaultWriteService(cfg.vault_root)
        return _service_singleton


def _paper_lock(paper_id: str) -> threading.RLock:
    with _paper_locks_guard:
        lock = _paper_locks.get(paper_id)
        if lock is None:
            lock = threading.RLock()
            _paper_locks[paper_id] = lock
        return lock


def _papers_root_ptr() -> Path:
    """Vault-relative location of the papers root.

    Fails closed. Returning ``Path(".")`` when the configuration is broken used
    to write files to the Vault root instead of the papers root — turning a
    serious misconfiguration into silent data placement in the wrong place.
    """
    cfg = get_cfg()
    root = cfg.papers_root_or_default
    try:
        return root.relative_to(cfg.vault_root)
    except ValueError as exc:
        raise HTTPException(
            500,
            "papers root is not inside the vault; refusing to resolve any paper path",
        ) from exc


def _paper_rel(paper: Paper, *parts: str) -> str:
    """Build a Vault-relative path for a file inside a paper folder.

    ``folder_relpath`` is relative to the **papers root**, while
    ``VaultWriteService`` resolves against the **vault root**. Conflating the
    two silently writes files to the wrong directory — e.g. a note would land
    in ``Vault/方向分类/...`` instead of ``Vault/论文根/方向分类/...``.
    This is the single place that joins them.
    """
    base = _papers_root_ptr()
    tail = Path(paper.folder_relpath).joinpath(*parts)
    return str(base / tail) if str(base) != "." else str(tail)


def _papers_root() -> Path:
    cfg = get_cfg()
    root = cfg.papers_root_or_default
    try:
        root.relative_to(cfg.vault_root)
    except ValueError as exc:  # pragma: no cover - config guard
        raise HTTPException(500, "papers root escapes the vault") from exc
    return root


# --------------------------------------------------------------------- models


class StatusUpdate(BaseModel):
    status: str = Field(..., description="UNREAD | READING | COMPLETED")
    reason: Optional[str] = None


class NoteCreate(BaseModel):
    content: str = ""
    rel_path: Optional[str] = None


class NoteSave(BaseModel):
    content: str
    expected_hash: str


class WorkspaceStateUpdate(BaseModel):
    active_pdf_source_id: Optional[str] = None
    active_markdown_source_id: Optional[str] = None
    active_pane: str = "PDF"
    source_positions: Optional[Dict[str, Dict[str, Any]]] = None
    note_cursor_start: Optional[int] = None
    note_cursor_end: Optional[int] = None
    note_content_sha256: Optional[str] = None
    #: Version the client last read. When supplied, a save is refused if the
    #: stored state has moved on, so two tabs cannot silently overwrite each
    #: other (last-write-wins).
    expected_state_version: Optional[int] = None


def _finite(value: Any) -> bool:
    """True for a real, finite number.

    NaN and Infinity are rejected explicitly: `float("inf")` passes an
    isinstance check but SQLite stores it as a non-JSON token, and the frontend
    then throws a SyntaxError parsing the response — a server-side value that
    crashes the client.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _validate_position(source_id: str, position: Any) -> Dict[str, Any]:
    """Validate one stored position against the frozen schema (ADR-007).

    Positions were previously persisted as free-form JSON, so a client could
    store `page_index: "seven"` and the breakage would only appear on restore.
    Validation belongs here rather than in the browser because this is the
    record every reader will trust.
    """
    if not isinstance(position, dict):
        raise HTTPException(400, f"position for {source_id} must be an object")

    kind = position.get("kind")
    if kind not in {"PDF", "MARKDOWN"}:
        raise HTTPException(400, f"position for {source_id} has unknown kind: {kind!r}")

    if kind == "PDF":
        page = position.get("page_index")
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            raise HTTPException(
                400, f"PDF position for {source_id} needs a non-negative integer page_index"
            )
        ratio = position.get("page_offset_ratio", 0)
        if not _finite(ratio) or not 0 <= ratio <= 1:
            raise HTTPException(
                400, f"PDF position for {source_id} needs page_offset_ratio within 0..1"
            )
        scale = position.get("scale", 1)
        if not _finite(scale) or scale <= 0:
            raise HTTPException(
                400, f"PDF position for {source_id} needs a finite positive scale"
            )
        rotation = position.get("rotation", 0)
        if rotation not in (0, 90, 180, 270):
            raise HTTPException(
                400, f"PDF position for {source_id} needs rotation in 0/90/180/270"
            )
    else:
        heading = position.get("heading_path", [])
        if not isinstance(heading, list) or any(not isinstance(h, str) for h in heading):
            raise HTTPException(
                400, f"Markdown position for {source_id} needs heading_path as a list of strings"
            )
        ratio = position.get("scroll_ratio", 0)
        if not _finite(ratio) or not 0 <= ratio <= 1:
            raise HTTPException(
                400, f"Markdown position for {source_id} needs scroll_ratio within 0..1"
            )

    # A position whose source version is unknown cannot be safely restored, so
    # the field is required rather than optional.
    version = position.get("source_version")
    if version is not None and (
        not isinstance(version, int) or isinstance(version, bool) or version < 0
    ):
        raise HTTPException(
            400, f"position for {source_id} needs source_version as a non-negative integer"
        )
    return position


# ------------------------------------------------------------------ helpers


def _paper_payload(paper: Paper, source_count: int = 0) -> Dict[str, Any]:
    status = paper.status
    # COMPLETED outranks everything; otherwise a first open implies READING.
    return {
        "paper_id": paper.paper_id,
        "title": paper.title,
        "display_title": paper.display_title,
        "folder_relpath": paper.folder_relpath,
        "category_relpath": paper.category_relpath,
        "binding_state": paper.binding_state.value,
        "status": status.value,
        "status_label": {"UNREAD": "未看", "READING": "正在看", "COMPLETED": "看完"}[status.value],
        "paper_tags": paper.paper_tags,
        "primary_pdf_source_id": paper.primary_pdf_source_id,
        "primary_translation_source_id": paper.primary_translation_source_id,
        "note_id": paper.note_id,
        "first_opened_at": paper.first_opened_at,
        "last_opened_at": paper.last_opened_at,
        "completed_at": paper.completed_at,
        "source_count": source_count,
        "inactive_at": paper.inactive_at,
    }


def _source_payload(source: PaperSource) -> Dict[str, Any]:
    return {
        "source_id": source.source_id,
        "paper_id": source.paper_id,
        "role": source.role.value,
        "role_label": {
            "ORIGINAL_PDF": "原文 PDF",
            "SUPPLEMENTAL_PDF": "附加 PDF",
            "TRANSLATION_FULL": "全文翻译",
            "TRANSLATION_GUIDE": "翻译导读",
            "EXTRACTED_MARKDOWN": "解析 Markdown",
            "OTHER_MARKDOWN": "其他 Markdown",
        }[source.role.value],
        "media_kind": source.media_kind.value,
        "rel_path": source.rel_path,
        "is_primary": source.is_primary,
        "source_version": source.source_version,
        "size_bytes": source.size_bytes,
        "active": source.active,
        "missing_since": source.missing_since,
    }


def _require_paper(storage: paper_storage.PaperStorage, paper_id: str) -> Paper:
    paper = storage.get_paper(paper_id)
    if paper is None:
        raise HTTPException(404, f"unknown paper: {paper_id}")
    return paper


def _adopt(
    storage: paper_storage.PaperStorage, paper: Paper, operation: str
) -> Paper:
    """Anchor identity in the Vault before any dependent state is written.

    This is the gate ADR-006 requires. Without it a paper's identity exists
    only in SQLite, so a rebuild detaches every note, annotation and reading
    position from its paper.
    """
    if operation not in DEPENDENT_OPERATIONS:
        raise HTTPException(500, f"internal: unknown adoption trigger {operation}")

    # Adoption writes the manifest ahead of whatever dependent state follows, so
    # a crash in between leaves a paper that is adopted but missing the step
    # that triggered adoption. The intent lets startup finish the job rather
    # than leaving it half-done.
    intent = None
    if not paper.manifest_relpath:
        intent = storage.begin_write_intent(
            paper.paper_id,
            "adopt",
            {
                "trigger": operation,
                # Recovery rejoins folder_relpath with this prefix; without
                # it the manifest lands beside the papers root.
                "papers_root_rel": str(_papers_root_ptr()),
            },
        )
    try:
        sources = storage.list_sources(paper.paper_id, include_inactive=True)
        base = _papers_root_ptr()
        paper = ensure_adopted(
            storage,
            _service(),
            paper,
            sources,
            operation=operation,
            papers_root_rel=str(base) if str(base) != "." else "",
        )
    except ManifestError as exc:
        if intent:
            storage.fail_write_intent(intent, str(exc))
        raise HTTPException(500, f"cannot adopt paper: {exc}") from exc
    storage.upsert_paper(paper, allow_folder_move=True)
    if intent and paper.manifest_relpath:
        storage.commit_write_intent(intent)
    return paper


# ------------------------------------------------------------------- papers


@router.get("/papers")
def list_papers(
    status: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
):
    storage = _storage()
    parsed_status = None
    if status:
        try:
            parsed_status = PaperStatus.parse(status)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    papers = storage.list_papers(status=parsed_status, category_relpath=category)
    payload = []
    for paper in papers:
        sources = storage.list_sources(paper.paper_id)
        payload.append(_paper_payload(paper, source_count=len(sources)))
    return _no_store({"papers": payload, "count": len(payload)})


@router.get("/papers/{paper_id}")
def get_paper(paper_id: str):
    storage = _storage()
    paper = _require_paper(storage, paper_id)
    return _no_store(_paper_payload(paper, source_count=len(storage.list_sources(paper_id))))


@router.get("/papers/{paper_id}/sources")
def list_sources(paper_id: str):
    storage = _storage()
    _require_paper(storage, paper_id)
    sources = storage.list_sources(paper_id, include_inactive=False)
    # Display order follows the documented preference: a full translation is
    # more useful than a guide, which beats a raw extraction (ADR-006).
    # Sorted purely by role rank — letting `is_primary` lead would put the
    # guide first whenever the scanner deemed the guide primary instead.
    sources.sort(key=lambda s: (s.role.display_rank, not s.is_primary))
    return _no_store({"sources": [_source_payload(s) for s in sources]})


@router.put("/papers/{paper_id}/status")
def set_status(paper_id: str, body: StatusUpdate):
    storage = _storage()
    paper = _require_paper(storage, paper_id)
    # Identity must be anchored before the reading state is recorded.
    paper = _adopt(storage, paper, "status_change")
    try:
        target = PaperStatus.parse(body.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Opening a finished paper must not silently downgrade it (ADR-007).
    if paper.status is PaperStatus.COMPLETED and target is PaperStatus.READING:
        storage.touch_last_opened(paper_id)
        return _no_store({"ok": True, "status": PaperStatus.COMPLETED.value, "downgraded": False})

    storage.set_status(paper_id, target, reason=body.reason)
    return _no_store({"ok": True, "status": target.value})


# --------------------------------------------------------------- workspace


@router.get("/papers/{paper_id}/workspace-state")
def get_workspace_state(paper_id: str):
    storage = _storage()
    _require_paper(storage, paper_id)
    state = storage.get_workspace_state(paper_id)
    if state is None:
        return _no_store({"paper_id": paper_id, "state": None})
    # `to_row()` yields the persisted column name; the API contract uses the
    # domain name so the frontend never sees a storage detail.
    row = dict(state.to_row())
    row["source_positions"] = row.pop("source_positions_json", "{}")
    if isinstance(row["source_positions"], str):
        import json as _json

        row["source_positions"] = _json.loads(row["source_positions"] or "{}")
    return _no_store({"paper_id": paper_id, "state": row})


@router.put("/papers/{paper_id}/workspace-state")
def save_workspace_state(paper_id: str, body: WorkspaceStateUpdate):
    from .models import WorkspaceState

    storage = _storage()
    paper = _require_paper(storage, paper_id)
    paper = _adopt(storage, paper, "workspace_state")
    existing = storage.get_workspace_state(paper_id)

    # Two tabs both read version 4 and both save: without this check the second
    # write silently wins and the first tab's reading position is lost.
    if body.expected_state_version is not None and existing is not None:
        if body.expected_state_version != existing.state_version:
            raise HTTPException(
                409,
                f"workspace state changed: expected version "
                f"{body.expected_state_version}, current {existing.state_version}",
            )

    # Merge, never replace: a paper with both a PDF and a translation keeps one
    # position per source, so sending only the PDF position must not erase the
    # Markdown one.
    positions = dict(existing.source_positions) if existing else {}
    if body.source_positions:
        for source_id, position in body.source_positions.items():
            positions[source_id] = _validate_position(source_id, position)

    # Every position must belong to a source of this paper, or the reader would
    # restore into a source it cannot open.
    for source_id in positions:
        source = storage.get_source(source_id)
        if source is None or source.paper_id != paper_id:
            raise HTTPException(
                400, f"position references a source that is not part of this paper: {source_id}"
            )

    # An active source must belong to this paper, otherwise the reader opens a
    # blank pane on restore and the cause is not obvious.
    for field_name, candidate in (
        ("active_pdf_source_id", body.active_pdf_source_id),
        ("active_markdown_source_id", body.active_markdown_source_id),
    ):
        if not candidate:
            continue
        source = storage.get_source(candidate)
        if source is None or source.paper_id != paper_id:
            raise HTTPException(400, f"{field_name} does not belong to this paper")

    state = WorkspaceState(
        paper_id=paper_id,
        active_pdf_source_id=body.active_pdf_source_id,
        active_markdown_source_id=body.active_markdown_source_id,
        active_pane=body.active_pane if body.active_pane in {"PDF", "MARKDOWN", "NOTE"} else "PDF",
        source_positions=positions,
        note_id=existing.note_id if existing else None,
        note_cursor_start=body.note_cursor_start,
        note_cursor_end=body.note_cursor_end,
        note_content_sha256=body.note_content_sha256,
        last_opened_at=utc_now(),
        updated_at=utc_now(),
        state_version=(existing.state_version + 1) if existing else 1,
    )
    storage.upsert_workspace_state(state)
    return _no_store({"ok": True, "state_version": state.state_version})


# --------------------------------------------------------------------- note


def _note_abs_path(paper: Paper, rel_path: str) -> Path:
    return _papers_root() / paper.folder_relpath / rel_path


@router.get("/papers/{paper_id}/note")
def get_note(paper_id: str):
    storage = _storage()
    paper = _require_paper(storage, paper_id)
    note = storage.get_note_for_paper(paper_id)
    if note is None:
        return _no_store({"paper_id": paper_id, "exists": False, "note": None})

    try:
        data, current_hash = _service().read(_paper_rel(paper, note.rel_path))
    except Exception as exc:
        return _no_store(
            {
                "paper_id": paper_id,
                "exists": False,
                "note": {"note_id": note.note_id, "rel_path": note.rel_path},
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    return _no_store(
        {
            "paper_id": paper_id,
            "exists": True,
            "note": {
                "note_id": note.note_id,
                "rel_path": note.rel_path,
                "content": data.decode("utf-8", errors="replace"),
                "hash": current_hash,
                "updated_at": note.updated_at,
            },
        }
    )


@router.post("/papers/{paper_id}/note")
def create_note(paper_id: str, body: NoteCreate):
    """Create the note on first write. A paper without a note is legal."""
    storage = _storage()
    paper = _require_paper(storage, paper_id)
    if storage.get_note_for_paper(paper_id) is not None:
        raise HTTPException(409, "note already exists for this paper")
    # The note will reference this paper, so the identity must already exist in
    # the Vault for a rebuild to be able to re-attach it.
    paper = _adopt(storage, paper, "note_creation")

    rel = body.rel_path or "notes.md"
    if not rel.endswith(".md"):
        raise HTTPException(400, "note path must end with .md")

    # One identity, generated once, written both to the note's frontmatter and
    # to SQLite. Generating it twice produced a note whose Vault-side id could
    # never match the database row, so the note could not be recovered from the
    # Vault after a rebuild — which defeats the point of storing it there.
    note_id = new_note_id()

    body_text = body.content if body.content else DEFAULT_NOTE_BODY.format(note_id=note_id, paper_id=paper.paper_id)
    content = _ensure_note_frontmatter(body_text, paper.paper_id, note_id)

    full_rel = _paper_rel(paper, rel)

    # The file is created before the database row. If the process dies in
    # between, the note exists in the Vault with no row — recoverable by
    # re-reading its frontmatter — rather than a row pointing at nothing.
    # The papers-root prefix is recorded so recovery can rebuild the path the
    # same way the write did; folder_relpath alone is relative to the papers
    # root, not the vault root.
    intent = storage.begin_write_intent(
        paper_id,
        "create_note",
        {
            "rel_path": rel,
            "note_id": note_id,
            "papers_root_rel": str(_papers_root_ptr()),
        },
    )
    try:
        result = _service().create(full_rel, content)
    except AlreadyExistsError as exc:
        storage.fail_write_intent(intent, "already exists")
        raise HTTPException(409, str(exc)) from exc
    except PathRejected as exc:
        storage.fail_write_intent(intent, str(exc))
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        storage.fail_write_intent(intent, str(exc))
        raise

    storage.upsert_note(
        PaperNote(
            note_id=note_id,
            paper_id=paper_id,
            rel_path=rel,
            content_sha256=result.new_hash,
        )
    )
    paper.note_id = note_id
    storage.upsert_paper(paper, allow_folder_move=True)

    # The adoption gate ran before this note had an id, so the manifest recorded
    # `note: null`. Without rewriting it, a database rebuild could not re-attach
    # the note — the identity property ADR-006 exists to provide.
    try:
        update_manifest(
            storage,
            _service(),
            paper,
            storage.list_sources(paper_id, include_inactive=True),
            papers_root_rel=str(_papers_root_ptr()),
            # The note's actual filename, so a custom name is recorded rather
            # than assumed to be notes.md.
            note_rel_path=rel,
        )
    except ManifestError as exc:
        # The note itself is safely on disk and in the row; a manifest failure
        # must not discard it, but it must be visible.
        logger.warning("note created but manifest not updated: %s", exc)

    storage.commit_write_intent(intent)

    return _no_store(
        {
            "ok": True,
            "note_id": note_id,
            "rel_path": rel,
            "hash": result.new_hash,
            "content": content,
        }
    )


DEFAULT_NOTE_BODY = (
    "---\n"
    "paper_id: {paper_id}\n"
    "paper_note_id: {note_id}\n"
    "paper_role: notes\n"
    "tags: []\n"
    "---\n\n"
    "# 阅读笔记\n\n"
    "## 核心内容\n\n"
    "## 关键结论\n\n"
    "## 我的理解\n\n"
    "## 创新点\n\n"
    "## 疑问\n\n"
    "## 可复用思想\n\n"
    "## 批注记录\n"
)


def _ensure_note_frontmatter(text: str, paper_id: str, note_id: str) -> str:
    """Guarantee the note carries its stable ids.

    Client-supplied content previously skipped the frontmatter entirely, so a
    note written from the editor had no paper_id and could not be re-identified
    after a rename. Status is deliberately never included: it is SQLite-owned
    and duplicating it into the Vault would create a second authority (ADR-007).
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            front = text[: end + 4]
            body = text[end + 4 :].lstrip("\n")
            additions = []
            if f"paper_id: {paper_id}" not in front:
                additions.append(f"paper_id: {paper_id}")
            if f"paper_note_id: {note_id}" not in front:
                additions.append(f"paper_note_id: {note_id}")
            if not additions:
                return text
            merged = front.rstrip()
            if merged.endswith("---"):
                merged = merged[:-3].rstrip()
            return merged + "\n" + "\n".join(additions) + "\n---\n\n" + body
    return f"---\npaper_id: {paper_id}\npaper_note_id: {note_id}\npaper_role: notes\n---\n\n{text}"


@router.put("/papers/{paper_id}/note")
def save_note(paper_id: str, body: NoteSave):
    storage = _storage()
    paper = _require_paper(storage, paper_id)
    note = storage.get_note_for_paper(paper_id)
    if note is None:
        raise HTTPException(404, "note not found; create it first")

    full_rel = _paper_rel(paper, note.rel_path)
    try:
        result = _service().save(full_rel, body.content, expected_hash=body.expected_hash)
    except ConflictError as exc:
        # 409 tells the client to keep its local draft and reload the remote
        # copy. Never auto-overwrite: the user may be editing in Obsidian.
        raise HTTPException(409, str(exc)) from exc
    except PathRejected as exc:
        raise HTTPException(400, str(exc)) from exc

    note.content_sha256 = result.new_hash
    note.updated_at = utc_now()
    storage.upsert_note(note)

    return _no_store(
        {
            "ok": True,
            "new_hash": result.new_hash,
            "previous_hash": result.previous_hash,
            "backup_path": result.backup_path,
        }
    )


# -------------------------------------------------------------- annotations


class AnnotationCreate(BaseModel):
    """Annotation creation payload.

    ``source_sha256`` and ``source_version`` are deliberately absent: the server
    derives them from the bound source. Accepting them from the client let a
    caller stamp a fabricated hash that no orphan check could ever contradict.
    """

    source_id: str
    kind: str
    anchor: Dict[str, Any]
    body_markdown: str = ""
    selected_text: Optional[str] = None
    #: Anchor layout version produced by the client. Defaults to the current
    #: version; older clients may still submit 1.
    anchor_schema_version: int = ANCHOR_SCHEMA_VERSION


def _sidecar_relpath(paper: Paper) -> str:
    return _paper_rel(paper, ANNOTATION_STORE_FILENAME)


class SidecarCorruptError(Exception):
    """The sidecar exists but cannot be parsed.

    Raised rather than degrading to an empty document: overwriting the bytes of
    a corrupt sidecar would destroy annotations the user may still be able to
    recover by hand, and would do so silently.
    """


def _read_sidecar(paper: Paper) -> Dict[str, Any]:
    """Read the authoritative annotation sidecar (ADR-008).

    A missing sidecar is legal: a paper with no annotations yet has none, and
    creating an empty one on read would write to the Vault without cause.

    An *unreadable* sidecar is not the same thing as a missing one and must
    fail closed.
    """
    rel = _sidecar_relpath(paper)
    try:
        data, digest = _service().read(rel)
    except Exception:
        # Genuinely absent: first annotation for this paper.
        return {
            "schema_version": 1,
            "paper_id": paper.paper_id,
            "annotations": [],
            "_hash": None,
            "_exists": False,
        }
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SidecarCorruptError(
            f"annotation sidecar is not valid JSON and must be repaired by hand: {rel}"
        ) from exc
    if not isinstance(parsed, dict):
        raise SidecarCorruptError(
            f"annotation sidecar is not a JSON object: {rel}"
        )
    parsed.setdefault("annotations", [])
    parsed["_hash"] = digest
    parsed["_exists"] = True
    return parsed


def _write_sidecar(paper: Paper, document: Dict[str, Any], expected_hash: Optional[str]) -> Any:
    payload = {k: v for k, v in document.items() if not k.startswith("_")}
    payload["schema_version"] = payload.get("schema_version", 1)
    payload["paper_id"] = paper.paper_id

    # Validate against the frozen schema before it reaches the Vault: a
    # malformed sidecar would be authoritative and unreadable at the same time.
    try:
        validate_annotations(payload)
    except Exception as exc:
        raise HTTPException(400, f"annotation document violates the frozen schema: {exc}") from exc

    rel = _sidecar_relpath(paper)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    service = _service()

    if expected_hash is None:
        try:
            return service.create(rel, text)
        except AlreadyExistsError:
            # The sidecar appeared between our read and our write. Re-read it
            # and report the new hash so the caller can re-apply its mutation
            # to the *current* document. Writing our stale payload here with
            # the fresh hash is exactly how a concurrent annotation gets
            # silently deleted.
            raise SidecarStaleError()
    try:
        return service.save(rel, text, expected_hash=expected_hash)
    except ConflictError as exc:
        raise SidecarStaleError() from exc


class SidecarStaleError(Exception):
    """The sidecar changed between load and save; the caller must retry."""


def _mutate_sidecar(paper: Paper, mutation: Any, *, attempts: int = 5) -> Tuple[Dict[str, Any], Any]:
    """Apply a mutation to the sidecar under a per-paper lock, with retry.

    The unit that must be atomic is load -> mutate -> save, not a single file
    write. Every retry re-reads the current document and re-applies the
    mutation, so two concurrent annotations both survive instead of the second
    one overwriting the first.
    """
    with _paper_lock(paper.paper_id):
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            document = _read_sidecar(paper)  # propagates SidecarCorruptError
            result = mutation(document)
            try:
                written = _write_sidecar(paper, document, document.get("_hash"))
                return document, written
            except SidecarStaleError as exc:
                last_error = exc
                continue
        raise HTTPException(
            409,
            "annotation store kept changing during the write; please retry",
        ) from last_error


def _reindex_annotations(storage: Any, paper: Paper, document: Dict[str, Any]) -> None:
    rows = []
    for item in document.get("annotations", []):
        anchor = item.get("anchor") or {}
        heading_path = anchor.get("heading_path")
        rows.append(
            {
                "annotation_id": item["annotation_id"],
                "source_id": item["source_id"],
                "kind": item["kind"],
                "anchor_type": anchor.get("type", "UNKNOWN"),
                "page_index": anchor.get("page_index"),
                "heading_path_json": json.dumps(heading_path, ensure_ascii=False)
                if heading_path
                else None,
                "selected_text": item.get("selected_text"),
                "body_markdown": item.get("body_markdown"),
                "source_sha256": item.get("source_sha256", "0" * 64),
                "source_version": item.get("source_version", 1),
                "orphaned_at": item.get("orphaned_at"),
                "deleted_at": item.get("deleted_at"),
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
        )
    storage.replace_annotations_index(paper.paper_id, rows)


@router.get("/papers/{paper_id}/annotations")
def list_annotations(paper_id: str):
    """List annotations, flattened for the client.

    The sidecar stores the locator nested under ``anchor``; the list view and
    the jump handler need a flat shape, and reading it from the sidecar keeps
    this endpoint authoritative instead of depending on the derived index. A
    corrupt sidecar is reported rather than silently presented as empty.
    """
    storage = _storage()
    paper = _require_paper(storage, paper_id)
    try:
        document = _read_sidecar(paper)
    except SidecarCorruptError as exc:
        return _no_store(
            {
                "paper_id": paper_id,
                "sidecar_hash": None,
                "corrupt": True,
                "error": str(exc),
                "annotations": [],
            }
        )

    return _no_store(
        {
            "paper_id": paper_id,
            "sidecar_hash": document.get("_hash"),
            "corrupt": False,
            "annotations": [_flatten_annotation(a) for a in document.get("annotations", [])],
        }
    )


def _flatten_annotation(record: Dict[str, Any]) -> Dict[str, Any]:
    """Project a sidecar record into the flat shape the client consumes.

    Emitting the raw record produced a shape mismatch: the client read
    ``anchor_type`` / ``page_index`` at the top level while they live under
    ``anchor``, so every locator label and jump silently did nothing.
    """
    anchor = record.get("anchor") or {}
    heading_path = anchor.get("heading_path")
    return {
        "annotation_id": record.get("annotation_id"),
        "source_id": record.get("source_id"),
        "kind": record.get("kind"),
        "body_markdown": record.get("body_markdown") or "",
        "selected_text": record.get("selected_text"),
        "anchor_type": anchor.get("type"),
        "page_index": anchor.get("page_index"),
        "heading_path": heading_path if isinstance(heading_path, list) else None,
        "text_quote": (anchor.get("text_quote") or {}).get("exact"),
        "anchor": anchor,
        "source_sha256": record.get("source_sha256"),
        "source_version": record.get("source_version"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "deleted_at": record.get("deleted_at"),
        "orphaned_at": record.get("orphaned_at"),
        "revision": record.get("revision", 1),
    }


@router.post("/papers/{paper_id}/annotations")
def create_annotation(paper_id: str, body: AnnotationCreate):
    storage = _storage()
    paper = _require_paper(storage, paper_id)

    if body.kind not in ANNOTATION_KINDS:
        raise HTTPException(400, f"unknown annotation kind: {body.kind}")

    # The sidecar records paper_id, so the identity must be in the Vault first.
    paper = _adopt(storage, paper, "annotation_creation")

    # The source identity, hash and version are derived from the bound source,
    # never taken from the client. Trusting them let a caller attach an
    # annotation to another paper's source, or stamp a fabricated hash that
    # no orphan check could ever contradict.
    source = storage.get_source(body.source_id)
    if source is None or not source.active:
        raise HTTPException(404, f"unknown or inactive source: {body.source_id}")
    if source.paper_id != paper_id:
        raise HTTPException(400, "source does not belong to this paper")

    annotation_id = new_annotation_id()
    now = utc_now()

    def mutation(document: Dict[str, Any]) -> None:
        record = {
            "annotation_id": annotation_id,
            "source_id": source.source_id,
            "kind": body.kind,
            "body_markdown": body.body_markdown or "",
            "selected_text": body.selected_text,
            # The version comes from the client so an older client can still
            # submit v1, but the server validates whichever it declares.
            "anchor_schema_version": int(body.anchor_schema_version),
            "anchor": body.anchor,
            "source_sha256": source.sha256 or ("0" * 64),
            "source_version": source.source_version or 1,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            "orphaned_at": None,
            "revision": 1,
        }
        document.setdefault("annotations", []).append(record)

    try:
        document, written = _mutate_sidecar(paper, mutation)
    except SidecarCorruptError as exc:
        raise HTTPException(409, str(exc)) from exc

    _reindex_annotations(storage, paper, document)
    return _no_store(
        {"ok": True, "annotation_id": annotation_id, "sidecar_hash": written.new_hash}
    )


@router.delete("/papers/{paper_id}/annotations/{annotation_id}")
def delete_annotation(paper_id: str, annotation_id: str):
    """Soft delete.

    ADR-002 applies here exactly as elsewhere: the record stays in the array
    with a ``deleted_at`` stamp so history is never destroyed.
    """
    storage = _storage()
    paper = _require_paper(storage, paper_id)

    box: Dict[str, Any] = {}

    def mutation(document: Dict[str, Any]) -> None:
        target = next(
            (
                a
                for a in document.get("annotations", [])
                if a.get("annotation_id") == annotation_id
            ),
            None,
        )
        if target is None:
            raise HTTPException(404, f"unknown annotation: {annotation_id}")
        target["deleted_at"] = utc_now()
        target["updated_at"] = target["deleted_at"]
        target["revision"] = int(target.get("revision", 1)) + 1
        box["deleted_at"] = target["deleted_at"]

    try:
        document, written = _mutate_sidecar(paper, mutation)
    except SidecarCorruptError as exc:
        raise HTTPException(409, str(exc)) from exc

    _reindex_annotations(storage, paper, document)
    return _no_store(
        {"ok": True, "deleted_at": box["deleted_at"], "sidecar_hash": written.new_hash}
    )


# ------------------------------------------------------------------ config


@router.get("/config")
def get_paper_config():
    """Expose only what the frontend needs to render the picker."""
    cfg = get_cfg()
    root = cfg.papers_root_or_default
    return _no_store(
        {
            "vault_root": str(cfg.vault_root),
            "papers_root": str(root),
            "papers_root_relpath": root.relative_to(cfg.vault_root).as_posix()
            if root != cfg.vault_root
            else ".",
            "max_depth": cfg.papers_max_depth,
        }
    )
