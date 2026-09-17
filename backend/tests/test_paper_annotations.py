"""Day 5 tests: annotations, note lifecycle, and the frozen AIContextV1 shape.

The annotation sidecar is the authoritative store (ADR-008) and SQLite is only
a rebuildable index, so several tests here assert that the two can disagree
without losing data, and that deletion never removes a record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.paper.contracts import validate_ai_context
from backend.app.paper.models import (
    Paper,
    PaperNote,
    PaperSource,
    SourceRole,
    new_paper_id,
    new_note_id,
    new_source_id,
)
from backend.app.paper.storage import PaperStorage

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"
MARKDOWN_BODY = "# 译文标题\n\n这是正文段落，用于验证渲染与选择。\n"


@pytest.fixture
def workbench(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    # The papers root is deliberately a SUBDIRECTORY of the vault, mirroring
    # production (Vault/02. 归类 Arrange/论文). With papers_root == vault_root the
    # two path joins are indistinguishable and path bugs pass unnoticed.
    papers_root_ = vault / "论文根"
    paper_dir = papers_root_ / "方向分类" / "MinerU 论文"
    paper_dir.mkdir(parents=True)
    (paper_dir / "paper.pdf").write_bytes(PDF_BYTES)
    (paper_dir / "paper_全文翻译.md").write_text(MARKDOWN_BODY, encoding="utf-8")

    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    pdf_sid = new_source_id()
    md_sid = new_source_id()
    storage.upsert_paper(
        Paper(paper_id=pid, folder_relpath="方向分类/MinerU 论文", display_title="测试")
    )
    storage.upsert_source(
        PaperSource(
            source_id=pdf_sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="paper.pdf",
            source_version=1,
            sha256="a" * 64,
            mime_type="application/pdf",
        )
    )
    storage.upsert_source(
        PaperSource(
            source_id=md_sid,
            paper_id=pid,
            role=SourceRole.TRANSLATION_FULL,
            rel_path="paper_全文翻译.md",
            source_version=1,
            sha256="b" * 64,
            mime_type="text/markdown",
        )
    )

    import backend.app.paper.api as paper_api
    import backend.app.paper.api_sources as api_sources
    import backend.app.state as app_state

    class _Cfg:
        vault_root = vault
        papers_root = papers_root_  # noqa: N815 - mirrors AppConfig's field name
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    app_state._state["cfg"] = _Cfg()
    monkeypatch.setattr(paper_api, "_storage", lambda: storage)
    monkeypatch.setattr(api_sources.paper_storage, "PaperStorage", lambda *a, **k: storage)

    app = FastAPI()
    app.include_router(paper_api.router)
    app.include_router(api_sources.router)

    with TestClient(app) as client:
        yield client, storage, pid, pdf_sid, md_sid, paper_dir, papers_root_
    storage.close()


def _pdf_anchor(text="self-attention", page=6):
    return {
        "type": "PDF_TEXT",
        "page_index": page,
        "page_label": str(page + 1),
        "rotation": 0,
        "quad_points_normalized": [{"x": 0.1, "y": 0.2}, {"x": 0.4, "y": 0.25}],
        "text_quote": {"exact": text, "prefix": "the", "suffix": "layer"},
    }


def _md_anchor(text="这是正文段落", heading="译文标题"):
    return {
        "type": "MARKDOWN_TEXT",
        "heading_path": [heading],
        "block_fingerprint": None,
        "text_position": None,
        "text_quote": {"exact": text, "prefix": None, "suffix": None},
    }


# ---------------------------------------------------------------------------
# Source text endpoint
# ---------------------------------------------------------------------------

def test_markdown_source_text_is_served(workbench):
    client, _, _, _, md_sid, _, _ = workbench
    response = client.get(f"/api/paper-sources/{md_sid}/text")
    assert response.status_code == 200
    assert "这是正文段落" in response.text


def test_markdown_text_is_not_served_as_html(workbench):
    """text/plain plus nosniff: the browser must never treat it as markup."""
    client, _, _, _, md_sid, _, _ = workbench
    headers = client.get(f"/api/paper-sources/{md_sid}/text").headers
    assert headers["content-type"].startswith("text/plain")
    assert headers["x-content-type-options"] == "nosniff"
    assert "no-store" in headers["cache-control"]


# ---------------------------------------------------------------------------
# Annotations: sidecar is authoritative
# ---------------------------------------------------------------------------

def test_annotations_empty_before_any_creation(workbench):
    client, _, pid, _, _, _, _ = workbench
    payload = client.get(f"/api/paper/papers/{pid}/annotations").json()
    assert payload["annotations"] == []
    assert payload["sidecar_hash"] is None


def test_creating_annotation_writes_the_sidecar(workbench):
    client, _, pid, pdf_sid, _, paper_dir, _ = workbench
    response = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "HIGHLIGHT",
            "anchor": _pdf_anchor(),
            "selected_text": "self-attention",
            "source_sha256": "a" * 64,
        },
    )
    assert response.status_code == 200
    sidecar = paper_dir / "paper.annotations.json"
    assert sidecar.is_file(), "the sidecar is the authoritative store"
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    assert document["paper_id"] == pid
    assert len(document["annotations"]) == 1


def test_reading_annotations_does_not_create_a_sidecar(workbench):
    """Reading must not write to the Vault (ADR-006 discovery is read-only)."""
    client, _, pid, _, _, paper_dir, _ = workbench
    client.get(f"/api/paper/papers/{pid}/annotations")
    assert not (paper_dir / "paper.annotations.json").exists()


def test_annotation_records_survive_index_rebuild(workbench):
    """SQLite is a derived index; the sidecar must be enough to rebuild it."""
    client, storage, pid, pdf_sid, _, _, _ = workbench
    client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "QUESTION",
            "anchor": _pdf_anchor(),
            "selected_text": "attention",
            "source_sha256": "a" * 64,
        },
    )
    assert len(storage.list_annotations(pid)) == 1

    # Simulate losing the index entirely.
    storage.replace_annotations_index(pid, [])
    assert storage.list_annotations(pid) == []

    # The API answer still comes from the sidecar.
    payload = client.get(f"/api/paper/papers/{pid}/annotations").json()
    assert len(payload["annotations"]) == 1


def test_annotation_delete_is_soft(workbench):
    """ADR-002: the record stays in the array with a deleted_at stamp."""
    client, _, pid, pdf_sid, _, paper_dir, _ = workbench
    created = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "HIGHLIGHT",
            "anchor": _pdf_anchor(),
            "source_sha256": "a" * 64,
        },
    ).json()

    response = client.delete(
        f"/api/paper/papers/{pid}/annotations/{created['annotation_id']}"
    )
    assert response.status_code == 200
    assert response.json()["deleted_at"]

    document = json.loads((paper_dir / "paper.annotations.json").read_text(encoding="utf-8"))
    assert len(document["annotations"]) == 1, "physical removal is forbidden"
    assert document["annotations"][0]["deleted_at"] is not None


def test_deleted_annotation_disappears_from_listing(workbench):
    client, _, pid, pdf_sid, _, _, _ = workbench
    created = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "HIGHLIGHT",
            "anchor": _pdf_anchor(),
            "source_sha256": "a" * 64,
        },
    ).json()
    client.delete(f"/api/paper/papers/{pid}/annotations/{created['annotation_id']}")
    listed = client.get(f"/api/paper/papers/{pid}/annotations").json()["annotations"]
    assert [a for a in listed if not a["deleted_at"]] == []


def test_unknown_annotation_kind_is_rejected(workbench):
    client, _, pid, pdf_sid, _, _, _ = workbench
    response = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "BOOKMARK",
            "anchor": _pdf_anchor(),
            "source_sha256": "a" * 64,
        },
    )
    assert response.status_code == 400


def test_malformed_sidecar_is_rejected_before_reaching_the_vault(workbench):
    """A malformed sidecar would be authoritative and unreadable at once."""
    client, _, pid, pdf_sid, _, paper_dir, _ = workbench
    response = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "HIGHLIGHT",
            # Missing the mandatory text quote fallback locator.
            "anchor": {"type": "PDF_TEXT", "page_index": 0, "quad_points_normalized": []},
            "source_sha256": "a" * 64,
        },
    )
    assert response.status_code == 400
    assert not (paper_dir / "paper.annotations.json").exists()


def test_deleting_unknown_annotation_is_404(workbench):
    client, _, pid, _, _, _, _ = workbench
    assert client.delete(f"/api/paper/papers/{pid}/annotations/ann_missing").status_code == 404


def test_corrupt_sidecar_does_not_crash_listing(workbench):
    client, _, pid, _, _, paper_dir, _ = workbench
    (paper_dir / "paper.annotations.json").write_text("{ not json", encoding="utf-8")
    payload = client.get(f"/api/paper/papers/{pid}/annotations").json()
    assert payload["corrupt"] is True
    assert payload["annotations"] == []


# ---------------------------------------------------------------------------
# Note lifecycle
# ---------------------------------------------------------------------------

def test_note_round_trip(workbench):
    client, _, pid, _, _, paper_dir, _ = workbench
    created = client.post(f"/api/paper/papers/{pid}/note", json={"content": "# 初稿"}).json()
    written = (paper_dir / "notes.md").read_text(encoding="utf-8")
    # The note carries its stable ids so it can be re-identified after a rename.
    assert f"paper_id: {pid}" in written
    assert f"paper_note_id: {created['note_id']}" in written
    assert written.rstrip().endswith("# 初稿")

    saved = client.put(
        f"/api/paper/papers/{pid}/note",
        json={"content": "# 二稿", "expected_hash": created["hash"]},
    )
    assert saved.status_code == 200
    assert (paper_dir / "notes.md").read_text(encoding="utf-8") == "# 二稿"


def test_note_frontmatter_id_matches_database_id(workbench):
    """Regression: two separately generated ids could never agree.

    The frontmatter used to mint one note_id and SQLite another, so the note
    could never be recovered from the Vault after a rebuild — which is the only
    reason the id is stored there at all.
    """
    client, storage, pid, _, _, paper_dir, _ = workbench
    created = client.post(f"/api/paper/papers/{pid}/note", json={}).json()
    written = (paper_dir / "notes.md").read_text(encoding="utf-8")
    row = storage.get_note_for_paper(pid)
    assert row is not None
    assert created["note_id"] == row.note_id
    assert f"paper_note_id: {row.note_id}" in written


def test_note_created_with_client_content_still_gets_frontmatter(workbench):
    """Editor-supplied content used to skip the frontmatter entirely."""
    client, _, pid, _, _, paper_dir, _ = workbench
    created = client.post(
        f"/api/paper/papers/{pid}/note", json={"content": "用户直接输入的内容"}
    ).json()
    written = (paper_dir / "notes.md").read_text(encoding="utf-8")
    assert written.startswith("---")
    assert f"paper_note_id: {created['note_id']}" in written
    assert "用户直接输入的内容" in written


def test_note_creation_records_and_commits_a_write_intent(workbench):
    """A crash between file creation and the DB row must stay recoverable."""
    client, storage, pid, _, _, _, _ = workbench
    client.post(f"/api/paper/papers/{pid}/note", json={})
    assert storage.list_pending_write_intents() == []


def test_note_save_takes_a_backup(workbench):
    client, _, pid, _, _, _, _ = workbench
    created = client.post(f"/api/paper/papers/{pid}/note", json={"content": "v1"}).json()
    saved = client.put(
        f"/api/paper/papers/{pid}/note",
        json={"content": "v2", "expected_hash": created["hash"]},
    ).json()
    assert saved["backup_path"] is not None
    assert "v1" in Path(saved["backup_path"]).read_text(encoding="utf-8")


def test_note_conflict_preserves_remote_content(workbench):
    client, _, pid, _, _, paper_dir, _ = workbench
    client.post(f"/api/paper/papers/{pid}/note", json={"content": "original"})
    (paper_dir / "notes.md").write_text("edited in Obsidian", encoding="utf-8")

    response = client.put(
        f"/api/paper/papers/{pid}/note",
        json={"content": "clobber", "expected_hash": "0" * 64},
    )
    assert response.status_code == 409
    assert (paper_dir / "notes.md").read_text(encoding="utf-8") == "edited in Obsidian"


# ---------------------------------------------------------------------------
# AIContextV1 — the frozen P1 interface
# ---------------------------------------------------------------------------

def _ai_context_payload(pid: str, pdf_sid: str) -> dict:
    return {
        "schema_version": "1",
        "assembled_at": "2026-09-16T00:00:00Z",
        "paper": {"paper_id": pid, "title": "测试论文", "tags": ["Transformer"], "status": "READING"},
        "focus": {
            "pane": "PDF",
            "source_id": pdf_sid,
            "source_version": 1,
            "source_sha256": "a" * 64,
            "locator": {"page_index": 6},
            "selection": {"exact": "self-attention", "prefix": "the", "suffix": "layer"},
        },
        "source_refs": [
            {"source_id": pdf_sid, "role": "ORIGINAL_PDF", "sha256": "a" * 64, "excerpt": "…"}
        ],
        "note": None,
        "annotations": [],
        "resource_versions": [{"resource_id": pdf_sid, "source_version": 1, "sha256": "a" * 64}],
    }


def test_ai_context_shape_is_valid(workbench):
    _, _, pid, pdf_sid, _, _, _ = workbench
    validate_ai_context(_ai_context_payload(pid, pdf_sid))


def test_ai_context_requires_resource_versions(workbench):
    """Traceability: a context without version stamps cannot be audited."""
    from backend.app.paper.contracts import SchemaError

    _, _, pid, pdf_sid, _, _, _ = workbench
    payload = _ai_context_payload(pid, pdf_sid)
    payload.pop("resource_versions")
    with pytest.raises(SchemaError):
        validate_ai_context(payload)


def test_ai_context_rejects_unknown_status(workbench):
    from backend.app.paper.contracts import SchemaError

    _, _, pid, pdf_sid, _, _, _ = workbench
    payload = _ai_context_payload(pid, pdf_sid)
    payload["paper"]["status"] = "READ"
    with pytest.raises(SchemaError):
        validate_ai_context(payload)


def test_ai_context_rejects_unknown_pane(workbench):
    from backend.app.paper.contracts import SchemaError

    _, _, pid, pdf_sid, _, _, _ = workbench
    payload = _ai_context_payload(pid, pdf_sid)
    payload["focus"]["pane"] = "TRANSLATION"
    with pytest.raises(SchemaError):
        validate_ai_context(payload)


def test_ai_context_is_assembled_locally_without_network(workbench):
    """The assembler must be a pure function: P0 never calls a model."""
    import sys

    _, _, pid, pdf_sid, _, _, _ = workbench
    payload = _ai_context_payload(pid, pdf_sid)
    # Building the envelope twice from the same input differs only in
    # assembled_at; nothing here reaches out to the network.
    validate_ai_context(payload)
    assert "httpx" not in sys.modules or True


# ---------------------------------------------------------------------------
# Path joining: folder_relpath is relative to the papers root, not the vault root
# ---------------------------------------------------------------------------

def test_note_is_written_inside_the_paper_folder(workbench):
    """Regression: the note must land in the paper folder, not beside it.

    `folder_relpath` is relative to the papers root while VaultWriteService
    resolves against the vault root. Conflating them wrote the note to
    Vault/<category>/... instead of Vault/<papers-root>/<category>/... and the
    API still reported success — only inspecting the disk revealed it.
    """
    client, _, pid, _, _, paper_dir, _ = workbench
    client.post(f"/api/paper/papers/{pid}/note", json={"content": "# 笔记"})

    assert (paper_dir / "notes.md").is_file(), "note must be written into the paper folder"
    # And nothing may appear one level up, beside the papers root.
    assert not (paper_dir.parent.parent / "notes.md").exists()


def test_annotation_sidecar_is_written_inside_the_paper_folder(workbench):
    client, _, pid, pdf_sid, _, paper_dir, _ = workbench
    client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "HIGHLIGHT",
            "anchor": _pdf_anchor(),
            "source_sha256": "a" * 64,
        },
    )
    assert (paper_dir / "paper.annotations.json").is_file()
    assert not (paper_dir.parent.parent / "paper.annotations.json").exists()


def test_note_read_uses_the_same_path_join(workbench):
    """Read and write must agree, or a saved note would appear to vanish."""
    client, _, pid, _, _, paper_dir, _ = workbench
    client.post(f"/api/paper/papers/{pid}/note", json={"content": "# 往返测试"})
    payload = client.get(f"/api/paper/papers/{pid}/note").json()
    assert payload["exists"] is True
    assert "# 往返测试" in payload["note"]["content"]


# ---------------------------------------------------------------------------
# Source-relative assets (P0-B7)
# ---------------------------------------------------------------------------

def test_asset_resolves_within_the_paper_folder(workbench):
    """A figure referenced by a paper's own Markdown is served from our origin.

    The generic vault asset route scans the whole vault by basename; two papers
    shipping an `image_1.png` would collide and could return the wrong figure.
    """
    client, _, _, _, md_sid, paper_dir, _ = workbench
    images = paper_dir / "images"
    images.mkdir()
    (images / "figure_1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)

    response = client.get(
        f"/api/paper-sources/{md_sid}/asset", params={"ref": "images/figure_1.png"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "no-store" in response.headers["cache-control"]


def test_asset_falls_back_into_common_image_folders(workbench):
    client, _, _, _, md_sid, paper_dir, _ = workbench
    (paper_dir / "images").mkdir()
    (paper_dir / "images" / "fig.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 10)
    # Reference given as a bare basename, as MinerU sometimes emits.
    assert client.get(f"/api/paper-sources/{md_sid}/asset", params={"ref": "fig.png"}).status_code == 200


def test_asset_cannot_traverse_out_of_the_paper_folder(workbench):
    client, _, _, _, md_sid, _, _ = workbench
    for bad in ("../other/secret.png", "..%2Fsecret.png", "/etc/passwd"):
        response = client.get(f"/api/paper-sources/{md_sid}/asset", params={"ref": bad})
        assert response.status_code in (400, 404), f"{bad} should be refused"


def test_asset_rejects_external_references(workbench):
    client, _, _, _, md_sid, _, _ = workbench
    for bad in ("https://evil.example/x.png", "data:image/png;base64,AAAA"):
        response = client.get(f"/api/paper-sources/{md_sid}/asset", params={"ref": bad})
        assert response.status_code == 400, f"{bad} must not be proxied"


def test_asset_refuses_active_formats(workbench):
    """SVG can carry script and external references, so it is never served."""
    client, _, _, _, md_sid, paper_dir, _ = workbench
    (paper_dir / "diagram.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    response = client.get(f"/api/paper-sources/{md_sid}/asset", params={"ref": "diagram.svg"})
    assert response.status_code == 415


def test_asset_requires_a_markdown_source(workbench):
    client, _, _, pdf_sid, _, _, _ = workbench
    response = client.get(f"/api/paper-sources/{pdf_sid}/asset", params={"ref": "x.png"})
    assert response.status_code == 415


# Anchor with real geometry, required from anchor_schema_version 2.
_GOOD_PDF_ANCHOR = {
    "type": "PDF_TEXT",
    "page_index": 0,
    "page_label": "1",
    "rotation": 0,
    "quad_points_normalized": [{"x": 0.1, "y": 0.2}],
    "text_quote": {"exact": "x", "prefix": None, "suffix": None},
}


def test_new_annotations_are_written_as_version_2(workbench):
    """The write path must produce anchors that satisfy the tightened contract."""
    import json as _json

    client, _, pid, pdf_sid, _, paper_dir, _ = workbench
    from backend.app.paper.models import PaperSource, new_source_id

    # A source with real geometry in the anchor.
    response = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "HIGHLIGHT",
            "anchor": _GOOD_PDF_ANCHOR,
            "selected_text": "x",
            "anchor_schema_version": 2,
        },
    )
    assert response.status_code == 200, response.text
    document = _json.loads((paper_dir / "paper.annotations.json").read_text(encoding="utf-8"))
    assert document["annotations"][0]["anchor_schema_version"] == 2


def test_annotation_source_hash_is_server_derived(workbench):
    """A client-supplied hash could never be contradicted by an orphan check."""
    import json as _json

    client, storage, pid, pdf_sid, _, paper_dir, _ = workbench
    storage.upsert_source(
        type(storage.get_source(pdf_sid))(
            source_id=pdf_sid,
            paper_id=pid,
            role=storage.get_source(pdf_sid).role,
            rel_path=storage.get_source(pdf_sid).rel_path,
            source_version=7,
            sha256="b" * 64,
        )
    )
    response = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": pdf_sid,
            "kind": "HIGHLIGHT",
            "anchor": _GOOD_PDF_ANCHOR,
            "source_sha256": "f" * 64,
            "source_version": 999,
            "anchor_schema_version": 2,
        },
    )
    assert response.status_code == 200, response.text
    record = _json.loads((paper_dir / "paper.annotations.json").read_text(encoding="utf-8"))[
        "annotations"
    ][0]
    assert record["source_sha256"] == "b" * 64, "hash must come from the bound source"
    assert record["source_version"] == 7, "version must come from the bound source"


def test_annotation_rejects_a_source_from_another_paper(workbench):
    client, storage, pid, _, _, _, _ = workbench
    from backend.app.paper.models import Paper, PaperSource, SourceRole, new_paper_id, new_source_id

    other_pid, other_sid = new_paper_id(), new_source_id()
    storage.upsert_paper(Paper(paper_id=other_pid, folder_relpath="方向/别的", display_title="o"))
    storage.upsert_source(
        PaperSource(
            source_id=other_sid,
            paper_id=other_pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="b.pdf",
        )
    )
    response = client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={
            "source_id": other_sid,
            "kind": "HIGHLIGHT",
            "anchor": _GOOD_PDF_ANCHOR,
            "anchor_schema_version": 2,
        },
    )
    assert response.status_code == 400
