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

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..state import get_cfg
from . import storage as paper_storage
from .models import (
    Paper,
    PaperNote,
    PaperSource,
    PaperStatus,
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


def _service() -> VaultWriteService:
    cfg = get_cfg()
    return VaultWriteService(cfg.vault_root)


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
    _require_paper(storage, paper_id)
    existing = storage.get_workspace_state(paper_id)

    state = WorkspaceState(
        paper_id=paper_id,
        active_pdf_source_id=body.active_pdf_source_id,
        active_markdown_source_id=body.active_markdown_source_id,
        active_pane=body.active_pane if body.active_pane in {"PDF", "MARKDOWN", "NOTE"} else "PDF",
        source_positions=body.source_positions or (existing.source_positions if existing else {}),
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
        data, current_hash = _service().read(
            str(Path(paper.folder_relpath) / note.rel_path)
        )
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

    rel = body.rel_path or "notes.md"
    if not rel.endswith(".md"):
        raise HTTPException(400, "note path must end with .md")

    full_rel = str(Path(paper.folder_relpath) / rel)
    # Seed the frontmatter with the stable ids so the note can be re-identified
    # if the file is renamed. Status is deliberately NOT written here: it is
    # SQLite-owned and must never be duplicated into the Vault (ADR-007).
    content = body.content or (
        "---\n"
        f"paper_id: {paper.paper_id}\n"
        f"paper_note_id: {new_note_id()}\n"
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

    try:
        result = _service().create(full_rel, content)
    except AlreadyExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PathRejected as exc:
        raise HTTPException(400, str(exc)) from exc

    note_id = new_note_id()
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

    return _no_store(
        {
            "ok": True,
            "note_id": note_id,
            "rel_path": rel,
            "hash": result.new_hash,
            "content": content,
        }
    )


@router.put("/papers/{paper_id}/note")
def save_note(paper_id: str, body: NoteSave):
    storage = _storage()
    paper = _require_paper(storage, paper_id)
    note = storage.get_note_for_paper(paper_id)
    if note is None:
        raise HTTPException(404, "note not found; create it first")

    full_rel = str(Path(paper.folder_relpath) / note.rel_path)
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
