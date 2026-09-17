"""Manifest read/write — the Vault-side identity authority (ADR-006).

The manifest is what makes a ``paper_id`` outlive the SQLite database. Without
it every paper's identity lives only in a local cache, and rebuilding that cache
silently severs reading state, notes and annotations from their paper.

Two rules shape this module:

* **Discovery stays read-only.** A scan must never write 282 manifests as a side
  effect. Writing happens only through :func:`adopt_paper`, which callers invoke
  when a paper is about to acquire dependent state.
* **A paper that already has dependent state must be adopted before that state
  is written**, otherwise the state references an identity that cannot be
  recovered from the Vault.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import MANIFEST_FILENAME
from .contracts import validate_manifest
from .models import (
    BindingOrigin,
    Paper,
    PaperSource,
    SourceRole,
    new_paper_id,
    new_source_id,
    utc_now,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdoptedIdentity:
    """Identity recorded in a manifest.

    A dataclass rather than a tuple because the field count grew once already
    (the note binding) and a positional tuple made that easy to drop silently —
    which is exactly what happened.
    """

    paper_id: str
    sources: List[Dict[str, Any]]
    note_path: Optional[str] = None
    note_id: Optional[str] = None

    def __iter__(self):
        """Iterate as (paper_id, sources, note_path, note_id).

        Keeps existing unpacking call sites working while the dataclass gives
        new code named access.
        """
        return iter((self.paper_id, self.sources, self.note_path, self.note_id))

    def __getitem__(self, index: int):
        return (self.paper_id, self.sources, self.note_path, self.note_id)[index]


class ManifestError(Exception):
    """The manifest cannot be read, parsed or trusted."""


class ManifestInvalidError(ManifestError):
    """The manifest exists but violates the frozen schema."""


def manifest_relpath(paper: Paper) -> str:
    """Manifest location relative to the paper folder."""
    return MANIFEST_FILENAME


def build_manifest(paper: Paper, sources: List[PaperSource]) -> Dict[str, Any]:
    """Serialise a paper and its bindings into a manifest document.

    Paths are stored relative to the manifest's own folder so the binding
    survives a folder rename or a move between categories.
    """
    now = utc_now()
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "paper_id": paper.paper_id,
        "title_override": paper.title_override,
        "sources": [
            {
                "source_id": source.source_id,
                "role": source.role.value,
                "path": source.rel_path,
                "primary": bool(source.is_primary),
                "active": bool(source.active),
            }
            for source in sources
        ],
        "note": (
            {"note_id": paper.note_id, "path": _note_name(paper)} if paper.note_id else None
        ),
        "annotation_store": "paper.annotations.json",
        "tags": list(paper.paper_tags),
        "created_at": paper.created_at or now,
        "updated_at": now,
        "inactive_at": paper.inactive_at,
    }
    if paper.external_ids:
        payload["external_ids"] = dict(paper.external_ids)
    return payload


def _note_name(paper: Paper) -> str:
    """Note filename. The manifest only records the name, never a full path."""
    return "notes.md"


def parse_manifest(
    document: Any,
) -> Tuple[str, List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Validate and unpack a manifest.

    Returns ``(paper_id, sources, note_path, note_id)``. Validating on read is
    what stops a hand-edited or half-written manifest from silently
    re-identifying a paper.

    The note id is returned alongside its path because the path alone cannot
    rebuild a binding: a database rebuild needs the stable id to re-attach the
    note, and dropping it here is what left the recovery chain half-connected.
    """
    try:
        validate_manifest(document)
    except Exception as exc:
        raise ManifestInvalidError(str(exc)) from exc

    sources = document.get("sources") or []
    note = document.get("note") or {}
    return document["paper_id"], sources, note.get("path"), note.get("note_id")


def manifest_to_sources(paper_id: str, entries: List[Dict[str, Any]]) -> List[PaperSource]:
    """Rebuild source bindings from a manifest.

    Sizes, hashes and versions are intentionally left unset: those are derived
    facts owned by SQLite and re-derived from the files on disk.
    """
    sources: List[PaperSource] = []
    for entry in entries:
        try:
            role = SourceRole(entry["role"])
        except (KeyError, ValueError) as exc:
            raise ManifestInvalidError(f"unknown source role: {entry.get('role')}") from exc
        sources.append(
            PaperSource(
                source_id=entry["source_id"],
                paper_id=paper_id,
                role=role,
                rel_path=entry["path"],
                rel_path_key_nfc=entry["path"],
                is_primary=bool(entry.get("primary")),
                active=bool(entry.get("active", True)),
                binding_origin=BindingOrigin.MANIFEST,
            )
        )
    return sources


def build_from_scratch(paper: Paper, sources: List[PaperSource]) -> Paper:
    """Assign identity to a paper that has none yet.

    Only called on the adoption path; discovery never reaches here.
    """
    if not paper.paper_id:
        paper.paper_id = new_paper_id()
    for source in sources:
        if not source.source_id:
            source.source_id = new_source_id()
        source.paper_id = paper.paper_id
    return paper


def reconcile_with_manifest(
    paper: Paper, sources: List[PaperSource], document: Dict[str, Any]
) -> Paper:
    """Trust the manifest over the scan for the fields it owns.

    Identity and binding are Vault-owned: if a scan disagrees with the manifest,
    the manifest wins, because that is the record that survives a database
    rebuild.
    """
    manifest_id, entries, note_path = parse_manifest(document)
    paper.paper_id = manifest_id
    paper.manifest_relpath = MANIFEST_FILENAME
    if document.get("title_override"):
        paper.title_override = document["title_override"]
    if isinstance(document.get("tags"), list):
        paper.paper_tags = list(document["tags"])
    if note_path:
        paper.note_id = paper.note_id or None
    return paper


# ---------------------------------------------------------------------------
# Adoption gate
# ---------------------------------------------------------------------------

ADOPTION_TRIGGERS = (
    "status_change",
    "note_creation",
    "annotation_creation",
    "tag_change",
    "manual_binding",
    "workspace_state",
)

#: Operations that create dependent state and therefore require a manifest.
DEPENDENT_OPERATIONS = frozenset(ADOPTION_TRIGGERS)


class AdoptionRequired(Exception):
    """Raised when dependent state would be written before identity is anchored."""

    def __init__(self, paper_id: str, operation: str):
        self.paper_id = paper_id
        self.operation = operation
        super().__init__(
            f"paper {paper_id} must be adopted before {operation} can write dependent state"
        )


def read_manifest_file(service: Any, rel_path: str) -> Optional[Dict[str, Any]]:
    """Read and validate a manifest, or return None when absent.

    A present-but-invalid manifest raises: treating it as absent would let a
    corrupt file cause a paper to be re-identified under a fresh UUID.
    """
    try:
        raw, _digest = service.read(rel_path)
    except Exception:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestInvalidError(f"manifest is not valid JSON: {rel_path}") from exc
    if not isinstance(document, dict):
        raise ManifestInvalidError(f"manifest is not a JSON object: {rel_path}")
    parse_manifest(document)  # schema validation
    return document


def is_adopted(paper: Paper) -> bool:
    """A paper is adopted once its manifest exists."""
    return bool(paper.manifest_relpath)


def ensure_adopted(
    storage: Any,
    service: Any,
    paper: Paper,
    sources: List[PaperSource],
    *,
    operation: str,
    papers_root_rel: str = "",
) -> Paper:
    """Guarantee the paper has a manifest before dependent state is written.

    This is the single gate for the whole subsystem. Every write path that
    creates state a rebuild could not otherwise re-attach to a paper must call
    it first — status changes, notes, annotations, tags and workspace state.

    ``papers_root_rel`` is the papers root expressed relative to the vault root.
    ``folder_relpath`` is relative to the papers root, while the write service
    resolves against the vault root; conflating the two writes the manifest
    beside the papers root instead of inside the paper folder.

    Idempotent: an already-adopted paper is returned unchanged, and a concurrent
    adoption of the same paper converges on the manifest that won.
    """
    if operation not in DEPENDENT_OPERATIONS:
        raise ValueError(f"unknown dependent operation: {operation}")

    if is_adopted(paper):
        return paper

    if not paper.paper_id:
        build_from_scratch(paper, sources)

    folder = paper.folder_relpath
    if papers_root_rel and papers_root_rel != ".":
        folder = f"{papers_root_rel.rstrip('/')}/{folder}"
    rel = f"{folder}/{MANIFEST_FILENAME}"
    document = build_manifest(paper, sources)

    try:
        result = service.create(rel, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        # Someone else adopted this paper first. Re-read and adopt their
        # manifest rather than overwriting it: two manifests for one paper is
        # exactly the duplicate-identity case that must fail closed.
        existing = read_manifest_file(service, rel)
        if existing is None:
            raise ManifestError(f"cannot adopt paper {paper.paper_id}: {exc}") from exc
        existing_id, entries, _note = parse_manifest(existing)
        if existing_id != paper.paper_id:
            raise ManifestInvalidError(
                f"adoption race produced two identities for {paper.folder_relpath}: "
                f"{paper.paper_id} vs {existing_id}"
            )
        paper.manifest_relpath = MANIFEST_FILENAME
        return paper

    paper.manifest_relpath = MANIFEST_FILENAME
    _ = result  # hash is not part of the paper row
    return paper


def load_adopted_identity(
    service: Any, folder_relpath: str, papers_root_rel: str = ""
) -> Optional[AdoptedIdentity]:
    """Read an existing manifest for a folder, if any.

    Used by the indexer so a rescan reuses the Vault-anchored identity instead
    of minting a new one — including the note binding, which the earlier version
    parsed and then discarded, leaving a rebuilt database unable to re-attach
    the note it had just written a manifest for.
    """
    folder = folder_relpath
    if papers_root_rel and papers_root_rel != ".":
        folder = f"{papers_root_rel.rstrip('/')}/{folder}"
    rel = f"{folder}/{MANIFEST_FILENAME}"
    document = read_manifest_file(service, rel)
    if document is None:
        return None
    paper_id, entries, note_path, note_id = parse_manifest(document)
    return AdoptedIdentity(
        paper_id=paper_id, sources=entries, note_path=note_path, note_id=note_id
    )


#: Per-path locks so concurrent manifest writers serialise instead of racing.
_MANIFEST_LOCKS: Dict[str, Any] = {}
_MANIFEST_LOCKS_GUARD = threading.Lock()


def _manifest_lock(path: str) -> Any:
    with _MANIFEST_LOCKS_GUARD:
        lock = _MANIFEST_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _MANIFEST_LOCKS[path] = lock
        return lock


def update_manifest(
    storage: Any,
    service: Any,
    paper: Paper,
    sources: List[PaperSource],
    *,
    papers_root_rel: str = "",
    attempts: int = 5,
) -> Paper:
    """Rewrite an adopted paper's manifest after its bindings change.

    `ensure_adopted` only writes when no manifest exists, which is correct for
    the adoption gate but wrong for the fields that keep changing afterwards. A
    note binding is the clear case: creation runs the gate before the note has an
    id, so the manifest was permanently left at `note: null` and a database
    rebuild could not re-attach the note — the exact property ADR-006 requires.

    Returns the paper unchanged when it is not adopted yet, because adoption is
    still the gate that decides when the manifest first appears.
    """
    if not is_adopted(paper):
        return paper

    base = papers_root_rel or _configured_prefix()
    rel = f"{base}/{paper.folder_relpath}/{MANIFEST_FILENAME}" if base else (
        f"{paper.folder_relpath}/{MANIFEST_FILENAME}"
    )
    document = build_manifest(paper, sources)

    existing = read_manifest_file(service, rel)
    if existing is None:
        # The manifest vanished (deleted externally). Re-create rather than
        # leave the paper unanchored.
        try:
            service.create(rel, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        except Exception as exc:  # noqa: BLE001
            raise ManifestError(f"cannot re-create manifest for {paper.paper_id}: {exc}") from exc
        return paper

    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    # Retry under a per-path lock. The optimistic hash makes a lost race
    # detectable, but detecting it is not the same as surviving it: without the
    # retry, concurrent writers fail rather than converge, which the concurrent
    # probe demonstrated (5 of 6 raised).
    with _manifest_lock(rel):
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            try:
                existing = read_manifest_file(service, rel)
            except ManifestInvalidError:
                raise
            if existing is None:
                try:
                    service.create(rel, payload)
                    return paper
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
            if existing == document:
                return paper
            try:
                _data, digest = service.read(rel)
                service.save(rel, payload, expected_hash=digest)
                return paper
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

    raise ManifestError(
        f"cannot update manifest for {paper.paper_id} after {attempts} attempts: {last_error}"
    )


def _configured_prefix() -> str:
    """Papers root relative to the vault root, from the live configuration."""
    try:
        from ..state import get_cfg

        cfg = get_cfg()
        relative = cfg.papers_root_or_default.relative_to(cfg.vault_root)
        return "" if str(relative) == "." else str(relative)
    except Exception:
        return ""
