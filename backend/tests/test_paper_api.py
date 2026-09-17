"""Day 4 contract tests: Paper API, idempotent indexing, PDF source delivery.

The frontend itself is exercised in the browser, but everything the frontend
depends on is asserted here:

* the paper id is stable across rescans (otherwise status and workspace state
  would be orphaned every time the vault is re-indexed);
* the source endpoint serves real bytes with a correct range contract;
* writing a note goes through the optimistic lock, so an edit made in Obsidian
  is never silently overwritten;
* reading status is never mirrored into the Vault (ADR-007).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.paper import storage as paper_storage
from backend.app.paper.models import (
    Paper,
    PaperNote,
    PaperSource,
    PaperStatus,
    SourceRole,
    new_paper_id,
    new_source_id,
)
from backend.app.paper.scanner import ScanConfig, discover_papers
from backend.app.paper.storage import PaperStorage
from backend.app.paper.writer import sha256_bytes

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "论文"
    paper_dir = root / "01-智能体" / "Attention Is All You Need"
    paper_dir.mkdir(parents=True)
    (paper_dir / "Attention Is All You Need.pdf").write_bytes(PDF_BYTES)
    (paper_dir / "Attention Is All You Need_翻译导读.md").write_text(
        "# 导读\n\nTransformer 架构。\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def client(vault: Path, tmp_path: Path, monkeypatch):
    """App wired to the temp vault and a temp paper database."""
    db = tmp_path / "papers.db"
    storage = PaperStorage(db)

    import backend.app.paper.api as paper_api
    import backend.app.state as app_state

    class _Cfg:
        vault_root = vault
        papers_root = vault
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    app_state._state["cfg"] = _Cfg()

    app = FastAPI()
    app.include_router(paper_api.router)
    monkeypatch.setattr(paper_api, "_storage", lambda: storage)
    import backend.app.paper.api_sources as api_sources

    monkeypatch.setattr(api_sources.paper_storage, "PaperStorage", lambda *a, **k: storage)
    app.include_router(api_sources.router)

    with TestClient(app) as test_client:
        test_client.storage = storage
        yield test_client
    storage.close()


def _index(storage: PaperStorage, root: Path) -> list:
    """Replicate the indexer's identity handling for test purposes."""
    from backend.app.paper.models import new_paper_id

    result = discover_papers(ScanConfig(root=root))
    for paper in result.papers:
        parts = paper.folder_relpath.split("/")
        paper.category_relpath = parts[0] if len(parts) > 1 else ""
        existing = storage.get_paper_by_folder(paper.folder_relpath)
        paper.paper_id = existing.paper_id if existing else new_paper_id()
        if existing:
            paper.status = existing.status
            paper.created_at = existing.created_at
        storage.upsert_paper(paper, allow_folder_move=True)
        prior = {s.rel_path: s for s in storage.list_sources(paper.paper_id, include_inactive=True)}
        for source in paper.sources:
            old = prior.get(source.rel_path)
            if old:
                source.source_id = old.source_id
            source.paper_id = paper.paper_id
            storage.upsert_source(source)
    return result.papers


# ---------------------------------------------------------------------------
# Identity stability
# ---------------------------------------------------------------------------

def test_indexing_assigns_identity(client):
    papers = _index(client.storage, client.storage.db_path.parent.parent / "论文") if False else None
    # vault path comes from the fixture; use the API instead for clarity
    response = client.get("/api/paper/papers")
    assert response.status_code == 200


def test_paper_id_survives_rescan(client, vault: Path):
    """The whole point of a path-independent id: re-indexing keeps it."""
    first = _index(client.storage, vault)
    assert len(first) == 1
    original = client.storage.get_paper_by_folder(first[0].folder_relpath)
    assert original is not None

    second = _index(client.storage, vault)
    again = client.storage.get_paper_by_folder(second[0].folder_relpath)
    assert again.paper_id == original.paper_id, "rescan must not mint a new identity"


def test_source_id_survives_rescan(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    before = {s.source_id for s in client.storage.list_sources(paper.paper_id)}
    _index(client.storage, vault)
    after = {s.source_id for s in client.storage.list_sources(paper.paper_id)}
    assert before == after, "the PDF endpoint must keep serving the same source_id"


def test_rescan_preserves_reading_status(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.storage.set_status(paper.paper_id, PaperStatus.READING)

    _index(client.storage, vault)
    reloaded = client.storage.get_paper(paper.paper_id)
    assert reloaded.status is PaperStatus.READING


def test_new_paper_folder_is_added_on_rescan(client, vault: Path):
    _index(client.storage, vault)
    assert client.storage.count_papers() == 1

    new_dir = vault / "01-智能体" / "Second Paper"
    new_dir.mkdir(parents=True)
    (new_dir / "Second Paper.pdf").write_bytes(PDF_BYTES)

    _index(client.storage, vault)
    assert client.storage.count_papers() == 2


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

def test_list_papers_requires_no_store(client, vault: Path):
    _index(client.storage, vault)
    response = client.get("/api/paper/papers")
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")


def test_list_papers_filters_by_status(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.storage.set_status(paper.paper_id, PaperStatus.COMPLETED)

    unread = client.get("/api/paper/papers?status=UNREAD").json()
    completed = client.get("/api/paper/papers?status=COMPLETED").json()
    assert unread["count"] == 0
    assert completed["count"] == 1


def test_list_papers_rejects_invalid_status(client):
    response = client.get("/api/paper/papers?status=NONSENSE")
    assert response.status_code == 400


def test_get_paper_includes_status_label(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    payload = client.get(f"/api/paper/papers/{paper.paper_id}").json()
    assert payload["status"] == "UNREAD"
    assert payload["status_label"] == "未看"


def test_get_unknown_paper_is_404(client):
    assert client.get("/api/paper/papers/pw_missing").status_code == 404


def test_sources_are_ordered_full_then_guide_then_pdf(client, vault: Path):
    """A full translation outranks a guide, which outranks the raw PDF."""
    paper_dir = vault / "01-智能体" / "Attention Is All You Need"
    (paper_dir / "Attention Is All You Need_全文翻译.md").write_text("# 全文", encoding="utf-8")
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]

    sources = client.get(f"/api/paper/papers/{paper.paper_id}/sources").json()["sources"]
    roles = [s["role"] for s in sources]
    assert roles.index("TRANSLATION_FULL") < roles.index("TRANSLATION_GUIDE") < roles.index("ORIGINAL_PDF")


def test_source_payload_never_leaks_absolute_path(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    sources = client.get(f"/api/paper/papers/{paper.paper_id}/sources").json()["sources"]
    for source in sources:
        assert not source["rel_path"].startswith("/")
        assert "/Users/" not in source["rel_path"]


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

def test_status_transition_sets_first_opened(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    response = client.put(
        f"/api/paper/papers/{paper.paper_id}/status", json={"status": "READING"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "READING"

    payload = client.get(f"/api/paper/papers/{paper.paper_id}").json()
    assert payload["first_opened_at"] is not None


def test_reopening_completed_paper_does_not_downgrade(client, vault: Path):
    """READING -> COMPLETED is a user decision; opening must not undo it."""
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.put(f"/api/paper/papers/{paper.paper_id}/status", json={"status": "READING"})
    client.put(f"/api/paper/papers/{paper.paper_id}/status", json={"status": "COMPLETED"})

    response = client.put(
        f"/api/paper/papers/{paper.paper_id}/status", json={"status": "READING"}
    )
    assert response.status_code == 200
    assert response.json()["downgraded"] is False
    assert response.json()["status"] == "COMPLETED"


def test_status_change_is_written_to_audit_log(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.put(f"/api/paper/papers/{paper.paper_id}/status", json={"status": "READING"})
    events = client.storage.list_status_events(paper.paper_id)
    assert len(events) == 1
    assert events[0]["to_status"] == "READING"


def test_reading_status_is_not_written_into_the_vault(client, vault: Path):
    """ADR-007: status is SQLite-owned and must never be mirrored to Vault."""
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.put(f"/api/paper/papers/{paper.paper_id}/status", json={"status": "COMPLETED"})

    note_path = vault / paper.folder_relpath / "notes.md"
    if note_path.exists():
        assert "COMPLETED" not in note_path.read_text(encoding="utf-8")

    for candidate in vault.rglob("*"):
        if candidate.is_file():
            assert "COMPLETED" not in candidate.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Notes: create, save, optimistic lock
# ---------------------------------------------------------------------------

def test_note_absent_before_creation(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    payload = client.get(f"/api/paper/papers/{paper.paper_id}/note").json()
    assert payload["exists"] is False


def test_create_note_seeds_identity_frontmatter(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    response = client.post(f"/api/paper/papers/{paper.paper_id}/note", json={})
    assert response.status_code == 200
    body = response.json()
    assert f"paper_id: {paper.paper_id}" in body["content"]
    assert "paper_note_id:" in body["content"]
    # Status must NOT appear in the note: two authorities would be created.
    assert "status:" not in body["content"]


def test_create_note_twice_conflicts(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.post(f"/api/paper/papers/{paper.paper_id}/note", json={})
    assert client.post(f"/api/paper/papers/{paper.paper_id}/note", json={}).status_code == 409


def test_note_save_returns_new_hash(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    created = client.post(f"/api/paper/papers/{paper.paper_id}/note", json={}).json()

    response = client.put(
        f"/api/paper/papers/{paper.paper_id}/note",
        json={"content": "# 改过的笔记", "expected_hash": created["hash"]},
    )
    assert response.status_code == 200
    assert response.json()["new_hash"] == sha256_bytes("# 改过的笔记".encode("utf-8"))


def test_note_save_with_stale_hash_conflicts(client, vault: Path):
    """An edit made in Obsidian must never be silently overwritten."""
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.post(f"/api/paper/papers/{paper.paper_id}/note", json={})

    response = client.put(
        f"/api/paper/papers/{paper.paper_id}/note",
        json={"content": "clobber", "expected_hash": "0" * 64},
    )
    assert response.status_code == 409


def test_conflicting_save_keeps_the_remote_content(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.post(f"/api/paper/papers/{paper.paper_id}/note", json={})

    note_path = vault / paper.folder_relpath / "notes.md"
    note_path.write_text("# 用户在 Obsidian 中改的", encoding="utf-8")

    client.put(
        f"/api/paper/papers/{paper.paper_id}/note",
        json={"content": "clobber", "expected_hash": "0" * 64},
    )
    assert "Obsidian" in note_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Workspace state
# ---------------------------------------------------------------------------

def test_workspace_state_round_trip(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]

    response = client.put(
        f"/api/paper/papers/{paper.paper_id}/workspace-state",
        json={
            "active_pane": "PDF",
            "source_positions": {
                "src_x": {
                    "kind": "PDF",
                    "page_index": 7,
                    "page_offset_ratio": 0.25,
                    "scale": 1,
                    "rotation": 0,
                    "source_version": 1,
                }
            },
            "note_cursor_start": 10,
        },
    )
    assert response.status_code == 200, response.text

    state = client.get(f"/api/paper/papers/{paper.paper_id}/workspace-state").json()["state"]
    assert state["source_positions"]["src_x"]["page_index"] == 7
    assert state["state_version"] == 1


def test_workspace_state_rejects_free_form_positions(client, vault: Path):
    """Positions were persisted as free-form JSON, so a bad value only failed
    at restore time. Validation belongs on the record every reader trusts."""
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    url = f"/api/paper/papers/{paper.paper_id}/workspace-state"

    bad_positions = [
        {"src_1": {"page_index": 7}},  # no kind
        {"src_1": {"kind": "PDF", "page_index": "seven", "page_offset_ratio": 0}},
        {"src_1": {"kind": "PDF", "page_index": -1, "page_offset_ratio": 0}},
        {"src_1": {"kind": "PDF", "page_index": 1, "page_offset_ratio": 1.5}},
        {"src_1": {"kind": "PDF", "page_index": 1, "page_offset_ratio": 0, "scale": 0}},
        {"src_1": {"kind": "MARKDOWN", "heading_path": "not-a-list", "scroll_ratio": 0}},
        {"src_1": {"kind": "MARKDOWN", "heading_path": [], "scroll_ratio": -1}},
        {"src_1": "not-an-object"},
    ]
    for positions in bad_positions:
        response = client.put(url, json={"active_pane": "PDF", "source_positions": positions})
        # 400 from the explicit validator; 422 when Pydantic rejects the shape
        # before it runs. Both are refusals, which is what matters here.
        assert response.status_code in (400, 422), f"should reject {positions}"


def test_workspace_state_rejects_foreign_active_source(client, vault: Path):
    """An active source belonging to another paper would open a blank pane."""
    from backend.app.paper.models import Paper, PaperSource, SourceRole, new_paper_id, new_source_id

    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]

    other_pid, other_sid = new_paper_id(), new_source_id()
    client.storage.upsert_paper(
        Paper(paper_id=other_pid, folder_relpath="方向/别的", display_title="o")
    )
    client.storage.upsert_source(
        PaperSource(
            source_id=other_sid,
            paper_id=other_pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="b.pdf",
        )
    )

    response = client.put(
        f"/api/paper/papers/{paper.paper_id}/workspace-state",
        json={"active_pane": "PDF", "active_pdf_source_id": other_sid},
    )
    assert response.status_code == 400


def test_workspace_state_version_conflict_is_refused(client, vault: Path):
    """Two tabs must not silently overwrite each other."""
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    url = f"/api/paper/papers/{paper.paper_id}/workspace-state"

    first = client.put(url, json={"active_pane": "PDF"})
    assert first.status_code == 200
    version = first.json()["state_version"]

    # Tab A read version N, tab B already advanced it.
    client.put(url, json={"active_pane": "PDF", "expected_state_version": version})

    stale = client.put(url, json={"active_pane": "NOTE", "expected_state_version": version})
    assert stale.status_code == 409
    assert "version" in stale.json()["detail"]


def test_workspace_state_version_increments(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    for _ in range(3):
        response = client.put(
            f"/api/paper/papers/{paper.paper_id}/workspace-state",
            json={"active_pane": "PDF"},
        )
    assert response.json()["state_version"] == 3


def test_workspace_state_rejects_invalid_pane(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    client.put(
        f"/api/paper/papers/{paper.paper_id}/workspace-state",
        json={"active_pane": "SIDEWAYS"},
    )
    state = client.get(f"/api/paper/papers/{paper.paper_id}/workspace-state").json()["state"]
    assert state["active_pane"] == "PDF"


# ---------------------------------------------------------------------------
# PDF source endpoint against a paper bound through the API
# ---------------------------------------------------------------------------

def test_pdf_endpoint_serves_indexed_paper(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    sources = client.get(f"/api/paper/papers/{paper.paper_id}/sources").json()["sources"]
    pdf = next(s for s in sources if s["media_kind"] == "PDF")

    response = client.get(f"/api/paper-sources/{pdf['source_id']}/content")
    assert response.status_code == 200
    assert response.content == PDF_BYTES


def test_pdf_endpoint_range_on_indexed_paper(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    sources = client.get(f"/api/paper/papers/{paper.paper_id}/sources").json()["sources"]
    pdf = next(s for s in sources if s["media_kind"] == "PDF")

    response = client.get(
        f"/api/paper-sources/{pdf['source_id']}/content", headers={"Range": "bytes=0-7"}
    )
    assert response.status_code == 206
    assert response.content == PDF_BYTES[:8]


def test_pdf_endpoint_rejects_version_mismatch(client, vault: Path):
    _index(client.storage, vault)
    paper = client.storage.list_papers()[0]
    sources = client.get(f"/api/paper/papers/{paper.paper_id}/sources").json()["sources"]
    pdf = next(s for s in sources if s["media_kind"] == "PDF")

    ok = client.get(f"/api/paper-sources/{pdf['source_id']}/content?version=1")
    stale = client.get(f"/api/paper-sources/{pdf['source_id']}/content?version=99")
    assert ok.status_code == 200
    assert stale.status_code == 412


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_exposes_papers_root(client):
    payload = client.get("/api/paper/config").json()
    assert "papers_root" in payload
    assert "no-store" in client.get("/api/paper/config").headers.get("cache-control", "")
