"""Regression tests for the P0 review findings (Sol job 46149f03).

Every test here corresponds to a specific defect the review reproduced with a
probe. They are written so that reverting the fix makes them fail — the earlier
path-join tests passed against broken code, which is exactly the failure mode
this file is meant to prevent.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.app.paper.manifest import (
    ensure_adopted,
    is_adopted,
    load_adopted_identity,
    manifest_to_sources,
)
from backend.app.paper.models import (
    Paper,
    PaperSource,
    PaperStatus,
    SourceRole,
    new_paper_id,
    new_source_id,
)
from backend.app.paper.storage import PaperStorage
from backend.app.paper.writer import (
    AlreadyExistsError,
    PathRejected,
    VaultWriteService,
)

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture
def vault() -> Path:
    root = (Path(tempfile.mkdtemp()) / "vault").resolve()
    (root / "论文根" / "方向" / "论文A").mkdir(parents=True)
    (root / "论文根" / "方向" / "论文A" / "a.pdf").write_bytes(PDF_BYTES)
    return root


@pytest.fixture
def service(vault: Path) -> VaultWriteService:
    return VaultWriteService(vault, backup_root=Path(tempfile.mkdtemp()) / "bk")


# ---------------------------------------------------------------------------
# writer.py — three defects
# ---------------------------------------------------------------------------

def test_save_of_missing_file_does_not_deadlock(service: VaultWriteService):
    """Defect: save() acquired a non-reentrant lock then called create()."""
    done = {}

    def worker():
        try:
            service.save("missing.md", "content", expected_hash=None, require_existing=False)
            done["r"] = "returned"
        except Exception as exc:  # noqa: BLE001
            done["r"] = f"{type(exc).__name__}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "save(require_existing=False) must not deadlock"
    assert done.get("r") == "returned"


def test_short_write_does_not_truncate(service: VaultWriteService, vault: Path):
    """Defect: a single os.write may legally write fewer bytes than requested."""
    payload = "abcdefghij"
    real_write = os.write
    with mock.patch("os.write", side_effect=lambda fd, data: real_write(fd, data[:3])):
        service.create("方向/论文A/short.md", payload)
    written = (vault / "论文A/short.md") if False else (vault / "方向/论文A/short.md")
    assert written.read_text(encoding="utf-8") == payload


def test_short_write_does_not_truncate_on_save(service: VaultWriteService, vault: Path):
    target = "方向/论文A/save.md"
    service.create(target, "v1")
    _, digest = service.read(target)
    real_write = os.write
    with mock.patch("os.write", side_effect=lambda fd, data: real_write(fd, data[:2])):
        service.save(target, "0123456789", expected_hash=digest)
    assert (vault / target).read_text(encoding="utf-8") == "0123456789"


def test_internal_symlink_to_excluded_area_is_rejected(service: VaultWriteService, vault: Path):
    """Defect: `alias -> .git` let 'alias/config' resolve into a rejected area.

    The old check walked up from the *resolved* path, where the symlink had
    already been followed and was therefore invisible.
    """
    (vault / ".git").mkdir()
    (vault / ".git" / "config").write_text("secret", encoding="utf-8")
    (vault / "alias").symlink_to(vault / ".git")

    with pytest.raises(PathRejected):
        service.resolve("alias/config")


def test_any_symlink_in_path_is_rejected(service: VaultWriteService, vault: Path):
    (vault / "论文根" / "real").mkdir()
    (vault / "论文根" / "link").symlink_to(vault / "论文根" / "real")
    with pytest.raises(PathRejected):
        service.resolve("论文根/link/notes.md")


def test_normal_paths_still_work_after_symlink_hardening(
    service: VaultWriteService, vault: Path
):
    result = service.create("方向/论文A/notes.md", "ok")
    assert result.created
    assert (vault / "方向/论文A/notes.md").read_text(encoding="utf-8") == "ok"


# ---------------------------------------------------------------------------
# manifest + adoption gate — P0-B1
# ---------------------------------------------------------------------------

def test_paper_is_not_adopted_before_dependent_state(vault: Path):
    storage = PaperStorage(Path(tempfile.mkdtemp()) / "p.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="t"))
    assert is_adopted(storage.get_paper(pid)) is False
    assert not (vault / "论文根" / "方向" / "论文A" / "paper.workbench.json").exists()


def test_adoption_writes_manifest_inside_the_paper_folder(vault: Path):
    storage = PaperStorage(Path(tempfile.mkdtemp()) / "p.db")
    service = VaultWriteService(vault)
    pid, sid = new_paper_id(), new_source_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="t"))
    storage.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="a.pdf",
            sha256="a" * 64,
        )
    )

    paper = ensure_adopted(
        storage,
        service,
        storage.get_paper(pid),
        storage.list_sources(pid),
        operation="status_change",
        papers_root_rel="论文根",
    )
    assert is_adopted(paper)

    manifest = vault / "论文根" / "方向" / "论文A" / "paper.workbench.json"
    assert manifest.is_file(), "manifest must land inside the paper folder"
    # And not beside the papers root, which is where a naive join puts it.
    assert not (vault / "方向" / "论文A" / "paper.workbench.json").exists()

    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["paper_id"] == pid
    assert len(document["sources"]) == 1


def test_identity_survives_a_database_rebuild(vault: Path):
    """The acceptance criterion: delete the database, keep the identity."""
    service = VaultWriteService(vault)
    storage = PaperStorage(Path(tempfile.mkdtemp()) / "a.db")
    pid, sid = new_paper_id(), new_source_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="t"))
    storage.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="a.pdf",
            sha256="a" * 64,
        )
    )
    ensure_adopted(
        storage,
        service,
        storage.get_paper(pid),
        storage.list_sources(pid),
        operation="status_change",
        papers_root_rel="论文根",
    )

    # A brand-new, empty database with only the Vault to go on.
    rebuilt = PaperStorage(Path(tempfile.mkdtemp()) / "b.db")
    identity = load_adopted_identity(service, "方向/论文A", papers_root_rel="论文根")
    assert identity is not None
    # load_adopted_identity returns a dataclass now (it also carries the note
    # binding); named access keeps this test independent of field order.
    recovered_id, entries = identity.paper_id, identity.sources
    assert recovered_id == pid, "rebuilt identity must match the original"

    rebuilt.upsert_paper(
        Paper(paper_id=recovered_id, folder_relpath="方向/论文A", display_title="t")
    )
    for source in manifest_to_sources(recovered_id, entries):
        rebuilt.upsert_source(source)
    assert len(rebuilt.list_sources(recovered_id)) == 1


def test_adoption_is_idempotent(vault: Path):
    storage = PaperStorage(Path(tempfile.mkdtemp()) / "p.db")
    service = VaultWriteService(vault)
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="t"))

    first = ensure_adopted(
        storage,
        service,
        storage.get_paper(pid),
        [],
        operation="status_change",
        papers_root_rel="论文根",
    )
    storage.upsert_paper(first, allow_folder_move=True)
    second = ensure_adopted(
        storage,
        service,
        storage.get_paper(pid),
        [],
        operation="annotation_creation",
        papers_root_rel="论文根",
    )
    assert second.paper_id == pid


def test_manifest_never_records_reading_status(vault: Path):
    """ADR-007: status is SQLite-owned; duplicating it would create a second
    authority, and the manifest is the record a rebuild reads."""
    storage = PaperStorage(Path(tempfile.mkdtemp()) / "p.db")
    service = VaultWriteService(vault)
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="t"))
    storage.set_status(pid, PaperStatus.COMPLETED)
    ensure_adopted(
        storage,
        service,
        storage.get_paper(pid),
        [],
        operation="status_change",
        papers_root_rel="论文根",
    )
    raw = (vault / "论文根" / "方向" / "论文A" / "paper.workbench.json").read_text(
        encoding="utf-8"
    )
    assert "COMPLETED" not in raw
    assert "status" not in json.loads(raw)


def test_corrupt_manifest_is_rejected_not_treated_as_absent(vault: Path):
    from backend.app.paper.manifest import ManifestInvalidError, load_adopted_identity

    service = VaultWriteService(vault)
    path = vault / "论文根" / "方向" / "论文A" / "paper.workbench.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ManifestInvalidError):
        load_adopted_identity(service, "方向/论文A", papers_root_rel="论文根")


# ---------------------------------------------------------------------------
# storage.py — missing-path scope
# ---------------------------------------------------------------------------

def test_marking_one_missing_source_does_not_flag_the_rest():
    """Defect: the old implementation stamped every source, then narrowed."""
    storage = PaperStorage(Path(tempfile.mkdtemp()) / "p.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="P", display_title="t"))
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        storage.upsert_source(
            PaperSource(
                source_id=new_source_id(),
                paper_id=pid,
                role=SourceRole.ORIGINAL_PDF,
                rel_path=name,
            )
        )

    storage.mark_sources_missing(pid, ["b.pdf"])

    flagged = {s.rel_path for s in storage.list_sources(pid) if s.missing_since}
    assert flagged == {"b.pdf"}, f"only b.pdf should be flagged, got {flagged}"


def test_clearing_a_tombstone_restores_the_source():
    storage = PaperStorage(Path(tempfile.mkdtemp()) / "p.db")
    pid, sid = new_paper_id(), new_source_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="P", display_title="t"))
    storage.upsert_source(
        PaperSource(
            source_id=sid, paper_id=pid, role=SourceRole.ORIGINAL_PDF, rel_path="a.pdf"
        )
    )
    storage.mark_sources_missing(pid, ["a.pdf"])
    assert storage.get_source(sid).missing_since is not None
    storage.clear_missing(sid)
    assert storage.get_source(sid).missing_since is None


# ---------------------------------------------------------------------------
# PDF bridge — second open must not hang
# ---------------------------------------------------------------------------

def test_bridge_resets_ready_state_between_opens():
    """Defect: `ready` was never reset, so a second open() timed out.

    Asserted on the source rather than in a browser: the invariant is that
    open() clears the previous generation's readiness.
    """
    bridge_js = (
        Path(__file__).resolve().parents[2] / "frontend" / "dist" / "paper" / "pdf-bridge.js"
    ).read_text(encoding="utf-8")
    assert "this._generation = ++this._generation" in bridge_js or "++this._generation" in bridge_js
    assert "this.ready = false;" in bridge_js
    assert "_detachViewerEvents" in bridge_js


def test_frontend_requests_the_text_endpoint_for_markdown():
    """Defect: the client asked /content while the server requires /text."""
    api_js = (
        Path(__file__).resolve().parents[2] / "frontend" / "dist" / "paper" / "api.js"
    ).read_text(encoding="utf-8")
    block = api_js[api_js.index("sourceText") : api_js.index("listAnnotations")]
    # Inspect only the executable line, ignoring the explanatory comment.
    fetch_line = next(line for line in block.splitlines() if "fetch(" in line)
    assert "/text`" in fetch_line, f"Markdown must be fetched from /text, got: {fetch_line.strip()}"
    assert "/content" not in fetch_line


def test_frontend_reads_flat_anchor_fields():
    """Defect: the client read flat fields while the server sent a nested anchor."""
    base = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "paper"
    main_js = (base / "main.js").read_text(encoding="utf-8")
    ann_js = (base / "annotations.js").read_text(encoding="utf-8")
    assert "heading_path_json" not in main_js, "API returns heading_path, not heading_path_json"
    assert "heading_path_json" not in ann_js


# ---------------------------------------------------------------------------
# P0-B6: frontend closed loops
# ---------------------------------------------------------------------------

def _paper_module(name: str) -> str:
    return (
        Path(__file__).resolve().parents[2] / "frontend" / "dist" / "paper" / name
    ).read_text(encoding="utf-8")


def test_workspace_state_tracker_is_wired_into_the_workbench():
    """Defect: the endpoints existed but nothing on the frontend ever called
    them, so a reading position was never recorded or restored."""
    main_js = _paper_module("main.js")
    assert "WorkspaceStateTracker" in main_js
    assert "workspaceState.loadFor" in main_js
    assert "_restorePosition" in main_js
    assert "notePdfPosition" in main_js


def test_workspace_state_flushes_on_switch_and_on_hide():
    tracker = _paper_module("workspace-state.js")
    assert "visibilitychange" in tracker
    assert "DEBOUNCE_MS = 750" in tracker
    assert "MAX_INTERVAL_MS = 5000" in tracker


def test_note_editor_pins_a_paper_epoch():
    """Defect: a late autosave from paper A could mutate paper B's editor."""
    note_js = _paper_module("note-pane.js")
    assert "_epoch" in note_js
    assert "loadFor" in note_js
    assert "stale-epoch" in note_js


def test_frontend_blocks_remote_images():
    """ADR-009: opening a paper must not contact a third-party server."""
    pane = _paper_module("markdown-pane.js")
    assert "_enforceEgressBoundary" in pane
    assert "paper-external-image" in pane


def test_indexer_reuses_manifest_source_ids(tmp_path: Path, monkeypatch):
    """Defect: the indexer only consulted SQLite, so after a database rebuild it
    minted fresh source ids and every saved reading position was orphaned.

    Behavioural: adopt a paper (writing a manifest), then index into a brand-new
    database and assert the source id matches the manifest rather than being new.
    """
    from backend.app.paper import scanner as scanner_mod
    from backend.scripts import paper_index as indexer

    root = tmp_path / "vault"
    paper_dir = root / "论文根" / "方向" / "论文A"
    paper_dir.mkdir(parents=True)
    (paper_dir / "a.pdf").write_bytes(PDF_BYTES)

    # Adopt first, using a throwaway database, so a manifest exists on disk.
    service = VaultWriteService(root, backup_root=tmp_path / "bk")
    seed = PaperStorage(tmp_path / "seed.db")
    pid, sid = new_paper_id(), new_source_id()
    seed.upsert_paper(Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="t"))
    seed.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="a.pdf",
            sha256="a" * 64,
        )
    )
    ensure_adopted(
        seed,
        service,
        seed.get_paper(pid),
        seed.list_sources(pid),
        operation="status_change",
        papers_root_rel="论文根",
    )
    seed.close()

    # Now index into a fresh database, as a rebuild would.
    fresh_db = tmp_path / "fresh.db"

    class _Cfg:
        vault_path = root
        vault_root = root
        papers_root = root / "论文根"
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    monkeypatch.setattr(indexer, "load_config", lambda: _Cfg())
    monkeypatch.setattr(indexer, "PaperStorage", lambda *a, **k: PaperStorage(fresh_db))

    indexer.index_papers(dry_run=False)

    rebuilt = PaperStorage(fresh_db)
    papers = rebuilt.list_papers()
    assert len(papers) == 1
    assert papers[0].paper_id == pid, "the manifest's paper id must win over a fresh one"
    sources = rebuilt.list_sources(pid)
    assert len(sources) == 1
    assert sources[0].source_id == sid, "the manifest's source id must be reused"
    rebuilt.close()


# ---------------------------------------------------------------------------
# P0-B5: anchor schema versioning and P0-B4: pinned file descriptor
# ---------------------------------------------------------------------------

def _annotations_doc(version: int, anchor: dict) -> dict:
    return {
        "schema_version": 1,
        "paper_id": "pw_3d9e1234-5678-4abc-89de-0123456789ab",
        "annotations": [
            {
                "annotation_id": "ann_11111111-2222-4333-8999-444444444444",
                "source_id": "src_a12f1234-5678-4abc-89de-0123456789ab",
                "kind": "HIGHLIGHT",
                "body_markdown": "",
                "selected_text": "x",
                "anchor_schema_version": version,
                "anchor": anchor,
                "source_sha256": "a" * 64,
                "source_version": 1,
                "created_at": "2026-09-16T00:00:00Z",
                "updated_at": "2026-09-16T00:00:00Z",
                "deleted_at": None,
                "orphaned_at": None,
                "revision": 1,
            }
        ],
    }


_EMPTY_PDF_ANCHOR = {
    "type": "PDF_TEXT",
    "page_index": 0,
    "page_label": "1",
    "rotation": 0,
    "quad_points_normalized": [],
    "text_quote": {"exact": "x", "prefix": None, "suffix": None},
}

_GOOD_PDF_ANCHOR = {
    "type": "PDF_TEXT",
    "page_index": 0,
    "page_label": "1",
    "rotation": 0,
    "quad_points_normalized": [{"x": 0.1, "y": 0.2}],
    "text_quote": {"exact": "x", "prefix": None, "suffix": None},
}


def test_v1_annotation_with_empty_geometry_still_validates():
    """History must not be invalidated by tightening the contract."""
    from backend.app.paper.contracts import validate_annotations

    validate_annotations(_annotations_doc(1, _EMPTY_PDF_ANCHOR))


def test_v2_annotation_requires_geometry():
    """Version 2 exists precisely because a placeholder anchor is useless."""
    from backend.app.paper.contracts import SchemaError, validate_annotations

    with pytest.raises(SchemaError) as exc:
        validate_annotations(_annotations_doc(2, _EMPTY_PDF_ANCHOR))
    assert "quad" in str(exc.value) or "geometry" in str(exc.value)


def test_v2_annotation_with_geometry_validates():
    from backend.app.paper.contracts import validate_annotations

    validate_annotations(_annotations_doc(2, _GOOD_PDF_ANCHOR))


def test_v2_markdown_anchor_requires_a_positional_locator():
    from backend.app.paper.contracts import SchemaError, validate_annotations

    bare = {
        "type": "MARKDOWN_TEXT",
        "heading_path": ["A"],
        "block_fingerprint": None,
        "text_position": None,
        "text_quote": {"exact": "x", "prefix": None, "suffix": None},
    }
    with pytest.raises(SchemaError):
        validate_annotations(_annotations_doc(2, bare))

    located = {**bare, "block_fingerprint": "md3-abc"}
    validate_annotations(_annotations_doc(2, located))


def test_range_stream_is_pinned_to_one_file_revision(tmp_path: Path):
    """Defect: the generator opened the path later, so a replacement between
    stat() and the first read could mix two revisions in one response."""
    from backend.app.paper.api_sources import _iter_fd_range, _open_pinned

    target = tmp_path / "doc.pdf"
    original = b"%PDF-1.4\n" + b"ORIGINAL" * 100 + b"\n%%EOF\n"
    target.write_bytes(original)

    fd, stat = _open_pinned(target)
    try:
        generator = _iter_fd_range(fd, 0, stat.st_size - 1)
        first = next(generator)
        # Replace the file while the response is still streaming.
        target.write_bytes(b"%PDF-1.4\n" + b"REPLACED" * 200 + b"\n%%EOF\n")
        rest = b"".join(generator)
    finally:
        os.close(fd)

    body = first + rest
    assert len(body) == len(original), "the pinned size must not change mid-stream"
    assert b"ORIGINAL" in body
    assert b"REPLACED" not in body, "bytes from two revisions must never mix"
