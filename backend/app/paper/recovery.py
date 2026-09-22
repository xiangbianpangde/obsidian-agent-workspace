"""Crash recovery for multi-file paper writes.

A paper write can touch more than one file — adopting a paper writes a manifest
with no note or sidecar, and creating a note writes the note after the manifest.
The filesystem has no cross-file transaction, so a crash between the steps
leaves partial state.

Sol's ruling on this was specific: recovery must **roll forward, never roll
back**. Deleting the note that was already created would destroy user content,
and the manifest that was already written is a correct manifest. So this module
finishes the work rather than undoing it.

The intent table exists for exactly this purpose; before this module it was
written to and never read, which is the "table built, nobody uses it" pattern
the review called out.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Paper, PaperNote, PaperSource, utc_now

logger = logging.getLogger(__name__)


def _papers_root_rel(payload: Dict[str, Any]) -> str:
    """Vault-relative prefix for the papers root.

    Prefers the value recorded in the intent, because that is what the
    interrupted write actually used, and falls back to the live configuration.
    """
    recorded = payload.get("papers_root_rel")
    if recorded:
        return str(recorded)
    return _configured_papers_root_rel()


@dataclass
class RecoveryOutcome:
    """What happened while recovering one intent."""

    intent_id: str
    paper_id: str
    operation: str
    resolved: bool
    action: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "paper_id": self.paper_id,
            "operation": self.operation,
            "resolved": self.resolved,
            "action": self.action,
            "detail": self.detail,
        }


@dataclass
class RecoveryReport:
    checked: int = 0
    resolved: int = 0
    unresolved: int = 0
    outcomes: List[RecoveryOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.unresolved == 0


def recover_pending_writes(storage: Any, service: Any) -> RecoveryReport:
    """Roll forward every pending write intent.

    Called at startup. Each intent is either completed or marked failed with a
    reason; nothing is deleted, and no partially written file is removed.
    """
    report = RecoveryReport()

    for intent in storage.list_pending_write_intents():
        report.checked += 1
        operation = intent.get("operation") or ""
        try:
            payload = json.loads(intent.get("payload_json") or "{}")
        except ValueError:
            payload = {}

        try:
            outcome = _recover_one(storage, service, intent, operation, payload)
        except Exception as exc:  # noqa: BLE001
            # A recovery failure must not stop the application from starting,
            # but it must be visible rather than silently dropped.
            logger.error("write-intent recovery failed for %s: %s", intent.get("intent_id"), exc)
            storage.fail_write_intent(intent["intent_id"], str(exc))
            outcome = RecoveryOutcome(
                intent_id=intent["intent_id"],
                paper_id=intent.get("paper_id") or "",
                operation=operation,
                resolved=False,
                action="failed",
                detail=str(exc),
            )

        report.outcomes.append(outcome)
        if outcome.resolved:
            report.resolved += 1
            if outcome.action in {"completed", "already-consistent"}:
                storage.commit_write_intent(intent["intent_id"])
        else:
            report.unresolved += 1

    return report


def _recover_one(
    storage: Any, service: Any, intent: Dict[str, Any], operation: str, payload: Dict[str, Any]
) -> RecoveryOutcome:
    paper_id = intent.get("paper_id") or ""
    intent_id = intent["intent_id"]

    paper: Optional[Paper] = storage.get_paper(paper_id)
    if paper is None:
        # The paper row is gone, so the intent cannot be completed. Report it
        # rather than guessing at a reconstruction.
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper_id,
            operation=operation,
            resolved=False,
            action="orphaned",
            detail="paper row no longer exists",
        )

    if operation == "create_note":
        return _recover_create_note(storage, service, intent_id, paper, payload)

    if operation == "adopt":
        return _recover_adopt(storage, service, intent_id, paper, payload)

    if operation == "create_annotation_store":
        return _recover_annotation_store(storage, service, intent_id, paper, payload)

    if operation == "resolve":
        return _recover_resolve(storage, service, intent_id, paper, payload)

    if operation == "rename_source":
        return _recover_rename_source(storage, service, intent_id, paper, payload)

    return RecoveryOutcome(
        intent_id=intent_id,
        paper_id=paper_id,
        operation=operation,
        resolved=False,
        action="unknown-operation",
        detail=f"no recovery handler for operation {operation!r}",
    )


def _locate_note_file(
    service: Any, paper: Paper, rel_path: str, recorded_prefix: str
) -> Optional[str]:
    """Find a note file mentioned by an intent, without trusting one prefix.

    Two constraints shape this, and the second is a security boundary rather
    than a convenience:

    1. The intent records the papers-root prefix, but a configuration change can
       make it stale. Trusting it blindly missed a file that existed, recovery
       concluded "neither the file nor the row was written", and committing that
       intent left a note on disk with no row.

    2. A note ALWAYS lives inside its paper folder. An earlier version fell back
       to a bare ``rel_path``, so a stray ``notes.md`` at the vault root was
       found for *every* paper: recovery built a row pointing at an unrelated
       file, the paper displayed someone else's content, and its real note could
       never be created (409, permanently).

    The containing check therefore asks whether the candidate's tail equals the
    paper folder plus the filename — which tolerates any prefix, while still
    refusing a file that is not under the paper folder.
    """
    folder_parts = Path(paper.folder_relpath).parts
    expected_tail = (*folder_parts, rel_path)

    candidates: list[str] = []
    for prefix in (recorded_prefix, _configured_papers_root_rel()):
        if prefix:
            candidates.append(str(Path(prefix, paper.folder_relpath, rel_path)))
    candidates.append(str(Path(paper.folder_relpath, rel_path)))

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not _ends_with_paper_note(candidate, expected_tail):
            continue
        try:
            service.read(candidate)
            return candidate
        except Exception:
            continue
    return None


def _ends_with_paper_note(candidate: str, expected_tail: tuple) -> bool:
    """True when the candidate path ends with the paper folder and filename.

    A prefix is allowed — the papers root sits between the vault root and the
    paper folder — but the tail must match exactly, so a same-named file
    elsewhere in the vault can never satisfy it.
    """
    candidate_parts = Path(candidate).parts
    if len(candidate_parts) < len(expected_tail):
        return False
    return tuple(candidate_parts[-len(expected_tail) :]) == expected_tail


def _configured_papers_root_rel() -> str:
    """Papers root relative to the vault root, from the live configuration."""
    try:
        from ..state import get_cfg

        cfg = get_cfg()
        relative = cfg.papers_root_or_default.relative_to(cfg.vault_root)
        return "" if str(relative) == "." else str(relative)
    except Exception:
        return ""


def _recover_create_note(
    storage: Any, service: Any, intent_id: str, paper: Paper, payload: Dict[str, Any]
) -> RecoveryOutcome:
    """Finish creating a note whose row may never have been written.

    Two crash points exist: the file was written but the row was not, or neither
    happened. Rolling forward means writing whichever side is missing — never
    deleting the file that already exists.
    """
    rel_path = payload.get("rel_path") or "notes.md"
    note_id = payload.get("note_id") or ""

    note_row = storage.get_note_for_paper(paper.paper_id)

    # Search for the file rather than assuming one prefix resolves it.
    located = _locate_note_file(service, paper, rel_path, payload.get("papers_root_rel") or "")
    file_exists = located is not None
    digest = None
    if located is not None:
        try:
            _data, digest = service.read(located)
        except Exception:
            file_exists = False

    if file_exists and note_row is not None:
        # Both sides present and the row points at the right note: nothing to
        # do. The interrupted step must have completed before the crash.
        if note_id and note_row.note_id != note_id:
            return RecoveryOutcome(
                intent_id=intent_id,
                paper_id=paper.paper_id,
                operation="create_note",
                resolved=False,
                action="inconsistent",
                detail=(
                    f"note file exists but the row records a different note_id "
                    f"({note_row.note_id} != {note_id}); refusing to guess"
                ),
            )
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="create_note",
            resolved=True,
            action="already-consistent",
            detail=f"note {note_row.note_id} present in both places",
        )

    if file_exists and note_row is None:
        # The file won; adopt it rather than deleting it. This is the roll
        # forward Sol required.
        if not note_id:
            return RecoveryOutcome(
                intent_id=intent_id,
                paper_id=paper.paper_id,
                operation="create_note",
                resolved=False,
                action="unrecoverable",
                detail="note file exists but the intent recorded no note_id",
            )
        storage.upsert_note(
            PaperNote(
                note_id=note_id,
                paper_id=paper.paper_id,
                rel_path=rel_path,
                content_sha256=digest,
            )
        )
        paper.note_id = note_id
        storage.upsert_paper(paper, allow_folder_move=True)
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="create_note",
            resolved=True,
            action="completed",
            detail=f"note row rebuilt from {located}",
        )

    if not file_exists and note_row is not None:
        # The row won. The file may have been removed externally; mark the note
        # as missing rather than recreating content we do not have.
        storage.mark_note_missing(note_row.note_id)
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="create_note",
            resolved=True,
            action="completed",
            detail="note row exists but the file is absent; marked as missing",
        )

    # Nothing found anywhere. That is genuinely indistinguishable from "the
    # write never happened", but the intent is not silently retired: it is
    # recorded as unresolvable so an operator can see that a file the intent
    # expected was never observed. Committing it would erase the only record
    # that a write was attempted.
    return RecoveryOutcome(
        intent_id=intent_id,
        paper_id=paper.paper_id,
        operation="create_note",
        resolved=False,
        action="no-evidence",
        detail=(
            f"no note file found for {paper.folder_relpath}/{rel_path} under any "
            "known prefix; the write may never have happened, or the file moved"
        ),
    )


def _recover_adopt(
    storage: Any, service: Any, intent_id: str, paper: Paper, payload: Dict[str, Any]
) -> RecoveryOutcome:
    """Adoption is idempotent by construction, so recovery is a re-run.

    `ensure_adopted` reads an existing manifest rather than overwriting it, so
    running it again converges. The import is local to keep this module free of
    a circular dependency on the manifest helpers.
    """
    from .manifest import MANIFEST_FILENAME, ensure_adopted, is_adopted

    if is_adopted(paper):
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="adopt",
            resolved=True,
            action="already-consistent",
            detail="manifest already present",
        )

    base = _papers_root_rel(payload)
    try:
        sources = storage.list_sources(paper.paper_id, include_inactive=True)
        recovered = ensure_adopted(
            storage,
            service,
            paper,
            sources,
            operation="status_change",
            papers_root_rel=base,
        )
        storage.upsert_paper(recovered, allow_folder_move=True)
    except Exception as exc:  # noqa: BLE001
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="adopt",
            resolved=False,
            action="failed",
            detail=str(exc),
        )

    _ = MANIFEST_FILENAME
    return RecoveryOutcome(
        intent_id=intent_id,
        paper_id=paper.paper_id,
        operation="adopt",
        resolved=True,
        action="completed",
        detail="manifest written during recovery",
    )


def _recover_annotation_store(
    storage: Any, service: Any, intent_id: str, paper: Paper, payload: Dict[str, Any]
) -> RecoveryOutcome:
    """An annotation sidecar is only ever replaced atomically.

    The sidecar is written through `VaultWriteService`, which publishes with a
    rename, so a crash leaves either the old file or the new one — never a
    half-written document. Nothing to roll forward; the intent exists only so a
    later reader knows a write was attempted.
    """
    from . import ANNOTATION_STORE_FILENAME

    base = _papers_root_rel(payload)
    full_rel = str(Path(base, paper.folder_relpath, ANNOTATION_STORE_FILENAME)) if base else str(
        Path(paper.folder_relpath, ANNOTATION_STORE_FILENAME)
    )
    try:
        service.read(full_rel)
        detail = "sidecar present; atomic publish means no partial state"
    except Exception:
        detail = "sidecar absent; the write never reached the rename"

    return RecoveryOutcome(
        intent_id=intent_id,
        paper_id=paper.paper_id,
        operation="create_annotation_store",
        resolved=True,
        action="already-consistent",
        detail=detail,
    )


def _recover_resolve(
    storage: Any, service: Any, intent_id: str, paper: Paper, payload: Dict[str, Any]
) -> RecoveryOutcome:
    """Recover an interrupted resolve operation (P0-5).

    If manifest was published to disk, reconcile canonical sources, note, tags
    and advance SQLite from AMBIGUOUS to ADOPTED in one transaction.
    """
    from . import MANIFEST_FILENAME
    from .manifest import manifest_to_sources, parse_manifest, read_manifest_file

    base = _papers_root_rel(payload)
    manifest_rel = str(Path(base, paper.folder_relpath, MANIFEST_FILENAME)) if base else str(
        Path(paper.folder_relpath, MANIFEST_FILENAME)
    )

    doc = None
    try:
        doc = read_manifest_file(service, manifest_rel)
    except Exception as exc:
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="resolve",
            resolved=False,
            action="failed",
            detail=f"cannot read manifest during recovery: {exc}",
        )

    if doc is None:
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="resolve",
            resolved=False,
            action="no-evidence",
            detail=f"no manifest found at {manifest_rel}; write never completed",
        )

    try:
        parsed = parse_manifest(doc)
        resolved_sources = manifest_to_sources(paper.paper_id, parsed.sources)
        title_override = doc.get("title_override")
        paper_tags = list(doc.get("tags") or [])
        note_id = parsed.note_id
        ext_ids = doc.get("external_ids") or {}

        storage.commit_resolved_adoption(
            paper.paper_id,
            resolved_sources,
            title_override=title_override,
            paper_tags=paper_tags,
            note_id=note_id,
            external_ids=ext_ids,
        )
    except Exception as exc:
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="resolve",
            resolved=False,
            action="failed",
            detail=f"commit_resolved_adoption failed: {exc}",
        )

    return RecoveryOutcome(
        intent_id=intent_id,
        paper_id=paper.paper_id,
        operation="resolve",
        resolved=True,
        action="completed",
        detail="manifest reconciled and SQLite rolled forward to ADOPTED",
    )


def _recover_rename_source(
    storage: Any, service: Any, intent_id: str, paper: Paper, payload: Dict[str, Any]
) -> RecoveryOutcome:
    """Recover an interrupted source rename write transaction (P0-G).

    If Manifest was published with the new path, roll forward SQLite.
    If Manifest was not published, retire intent without modifying SQLite.
    """
    from . import MANIFEST_FILENAME
    from .manifest import parse_manifest, read_manifest_file

    base = _papers_root_rel(payload)
    manifest_rel = str(Path(base, paper.folder_relpath, MANIFEST_FILENAME)) if base else str(
        Path(paper.folder_relpath, MANIFEST_FILENAME)
    )

    doc = None
    try:
        doc = read_manifest_file(service, manifest_rel)
    except Exception as exc:
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="rename_source",
            resolved=False,
            action="failed",
            detail=f"cannot read manifest during rename recovery: {exc}",
        )

    if doc is None:
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="rename_source",
            resolved=False,
            action="no-evidence",
            detail=f"no manifest found at {manifest_rel}; rename write never completed",
        )

    new_path = payload.get("new_path")
    old_path = payload.get("old_path")
    source_id = payload.get("source_id")

    parsed = parse_manifest(doc)
    has_new_path = any(s.get("path") == new_path for s in parsed.sources)

    if not has_new_path:
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="rename_source",
            resolved=False,
            action="no-evidence",
            detail=f"manifest does not contain new path {new_path}; rename never landed",
        )

    expected_digest = payload.get("published_manifest_digest")
    if expected_digest:
        try:
            _raw, cur_digest = service.read(manifest_rel)
        except Exception as exc:  # noqa: BLE001
            return RecoveryOutcome(
                intent_id=intent_id,
                paper_id=paper.paper_id,
                operation="rename_source",
                resolved=False,
                action="failed",
                detail=f"cannot re-read manifest to verify published digest: {exc}",
            )
        if cur_digest != expected_digest:
            # The manifest is not the document this intent published, so the
            # binding changed underneath the rename. Committing would relabel
            # unknown content as this rename's result.
            return RecoveryOutcome(
                intent_id=intent_id,
                paper_id=paper.paper_id,
                operation="rename_source",
                resolved=False,
                action="digest-mismatch",
                detail="manifest on disk is not the document this intent published",
            )

    try:
        from .manifest import manifest_to_sources
        from .models import BindingOrigin, BindingState, MediaKind, SourceRole

        paper_dir = (
            Path(service.vault_root, base, paper.folder_relpath)
            if base
            else Path(service.vault_root, paper.folder_relpath)
        )
        resolved_sources = manifest_to_sources(paper.paper_id, parsed.sources)
        missing_active = False

        # P0-P: Monotonically preserve source_version from SQLite prior!
        old_prior = storage.get_source(source_id) if source_id else None

        for s in resolved_sources:
            t = paper_dir / s.rel_path
            if t.is_file():
                st = t.stat()
                s.size_bytes = st.st_size
                s.mtime_ns = st.st_mtime_ns
                from .scanner import _sha256_file

                s.sha256 = _sha256_file(t)
                if s.source_id == source_id and old_prior:
                    if old_prior.sha256 == s.sha256:
                        s.source_version = old_prior.source_version
                    else:
                        s.source_version = old_prior.source_version + 1
            elif s.active:
                missing_active = True

        target_state = BindingState.DEGRADED if missing_active else BindingState.ADOPTED

        storage.commit_resolved_adoption(
            paper.paper_id,
            resolved_sources,
            title_override=doc.get("title_override"),
            paper_tags=list(doc.get("tags") or []),
            note_id=parsed.note_id,
            external_ids=doc.get("external_ids") or {},
            binding_state=target_state,
        )

        if old_path:
            from .models import SourceRole, new_source_id

            role = resolved_sources[0].role if resolved_sources else SourceRole.ORIGINAL_PDF
            old_retired_source = PaperSource(
                source_id=new_source_id(),
                paper_id=paper.paper_id,
                role=role,
                rel_path=old_path,
                is_primary=False,
                binding_origin=BindingOrigin.MANIFEST,
                active=False,
                missing_since=utc_now(),
            )
            storage.upsert_source(old_retired_source)
            storage.deactivate_source_by_path(paper.paper_id, old_path)
    except Exception as exc:
        return RecoveryOutcome(
            intent_id=intent_id,
            paper_id=paper.paper_id,
            operation="rename_source",
            resolved=False,
            action="failed",
            detail=f"commit_resolved_adoption failed during rename recovery: {exc}",
        )

    return RecoveryOutcome(
        intent_id=intent_id,
        paper_id=paper.paper_id,
        operation="rename_source",
        resolved=True,
        action="completed",
        detail=f"rename rolled forward for {new_path} with aggregate state updated to {target_state.value}",
    )
