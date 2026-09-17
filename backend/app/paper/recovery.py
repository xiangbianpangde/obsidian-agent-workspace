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

    Recovery prefers the value recorded in the intent, because that is what the
    interrupted write actually used. When it is absent (an older intent, or a
    caller that did not record it) the live configuration is consulted rather
    than assuming an empty prefix — assuming empty would write beside the papers
    root instead of inside the paper folder, which is the path bug this
    subsystem has already produced three times.
    """
    recorded = payload.get("papers_root_rel")
    if recorded:
        return str(recorded)
    try:
        from ..state import get_cfg

        cfg = get_cfg()
        root = cfg.papers_root_or_default
        relative = root.relative_to(cfg.vault_root)
        return "" if str(relative) == "." else str(relative)
    except Exception:
        return ""


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
    base = _papers_root_rel(payload)
    full_rel = str(Path(base, paper.folder_relpath, rel_path)) if base else str(
        Path(paper.folder_relpath, rel_path)
    )

    note_row = storage.get_note_for_paper(paper.paper_id)

    # Did the file land?
    try:
        data, digest = service.read(full_rel)
        file_exists = True
    except Exception:
        data, digest = b"", None
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
            detail=f"note row rebuilt from the file on disk ({note_id})",
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

    # Neither side exists: the crash happened before the first write, so the
    # intent never took effect and there is nothing to finish.
    return RecoveryOutcome(
        intent_id=intent_id,
        paper_id=paper.paper_id,
        operation="create_note",
        resolved=True,
        action="completed",
        detail="neither the file nor the row was written; intent had no effect",
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
