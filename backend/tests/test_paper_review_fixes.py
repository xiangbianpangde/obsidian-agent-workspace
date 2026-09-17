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
    recovered_id, entries = identity
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
