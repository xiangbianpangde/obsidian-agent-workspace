"""Paper Workbench subsystem (P0).

Domain model and authority split are frozen by ADR-006 / ADR-007 / ADR-008 / ADR-009:

- ``Paper`` is the single first-class aggregate. PDF, translation, note, tags,
  annotations, reading status and workspace state are all views or attachments.
- ``Paper`` 1:N ``PaperSource`` — a paper may legally own multiple PDFs and
  multiple translation variants (guide / full / MinerU extraction).
- Vault owns identity, bindings, tags, notes and annotations.
- SQLite owns reading status, reading metadata and workspace state.
- Every resource API is keyed by ASCII opaque IDs, never by Vault paths.

Schemas live in ``schemas/`` and are the frozen contracts for the manifest,
the annotation sidecar and the P1 AIContext envelope.
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

MANIFEST_SCHEMA_PATH = SCHEMA_DIR / "paper.workbench.schema.json"
ANNOTATIONS_SCHEMA_PATH = SCHEMA_DIR / "paper.annotations.schema.json"
AI_CONTEXT_SCHEMA_PATH = SCHEMA_DIR / "ai-context.v1.schema.json"

MANIFEST_FILENAME = "paper.workbench.json"
ANNOTATION_STORE_FILENAME = "paper.annotations.json"
DEFAULT_NOTE_FILENAME = "notes.md"

__all__ = [
    "SCHEMA_DIR",
    "MANIFEST_SCHEMA_PATH",
    "ANNOTATIONS_SCHEMA_PATH",
    "AI_CONTEXT_SCHEMA_PATH",
    "MANIFEST_FILENAME",
    "ANNOTATION_STORE_FILENAME",
    "DEFAULT_NOTE_FILENAME",
]
