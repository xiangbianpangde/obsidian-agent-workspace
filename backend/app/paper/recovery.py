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

from .models import Paper, PaperNote, utc_now

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

    The intent records the papers-root prefix, but a configuration change can
    make that stale. Trusting it blindly produced a real hazard: with a stale
    prefix the lookup missed a file that existed, recovery concluded "neither
    the file nor the row was written", and committing that intent would leave a
    note on disk with no row — invisible to the reader, and unrecoverable
    without noticing the orphan.

    So every plausible prefix is tried: the one the intent recorded, the one the
    live configuration implies, and none at all. Finding the file under any of
    them means the write did happen.
    """
    candidates: list[str] = []
    for prefix in (recorded_prefix, _configured_papers_root_rel()):
        if prefix:
            candidates.append(str(Path(prefix, paper.folder_relpath, rel_path)))
    # The paper folder without any prefix, and the bare filename, cover the case
    # where both the prefix and the folder segment were lost.
    candidates.append(str(Path(paper.folder_relpath, rel_path)))
    candidates.append(rel_path)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            service.read(candidate)
            return candidate
        except Exception:
            continue
    return None


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
