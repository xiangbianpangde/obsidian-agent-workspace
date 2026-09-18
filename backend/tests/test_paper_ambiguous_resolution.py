"""Acceptance tests for AMBIGUOUS resolution and the P0 reviewer probes (ADR-006 / ADR-007)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.paper import MANIFEST_FILENAME
from backend.app.paper.api import router as paper_router
from backend.app.paper.manifest import ensure_adopted, read_manifest_file, ManifestError
from backend.app.paper.models import (
    BindingOrigin,
    BindingState,
    Paper,
    PaperSource,
    PaperStatus,
    SourceRole,
    new_paper_id,
    new_source_id,
)
from backend.app.paper.recovery import recover_pending_writes
from backend.app.paper.scanner import ScanConfig, discover_papers
from backend.app.paper.storage import PaperStorage
from backend.app.paper.writer import VaultWriteService
from backend.scripts import paper_index as indexer

PDF_SAMPLE = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    papers = vault / "论文"
    papers.mkdir(parents=True)
    db_path = tmp_path / "papers.db"

    class _Cfg:
        vault_root = vault
        papers_root = papers
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    import backend.app.paper.api as paper_api
    import backend.app.state as app_state

    cfg = _Cfg()
    monkeypatch.setattr(app_state, "_state", {"cfg": cfg}, raising=False)
    monkeypatch.setattr(indexer, "load_config", lambda: cfg)
    monkeypatch.setattr(indexer, "PaperStorage", lambda *a, **k: PaperStorage(db_path))
    monkeypatch.setattr(paper_api, "_storage", lambda: PaperStorage(db_path))
    monkeypatch.setattr(paper_api, "_service", lambda: VaultWriteService(vault))

    app = FastAPI()
    app.include_router(paper_router)
    client = TestClient(app)

    return {
        "vault": vault,
        "papers": papers,
        "db": db_path,
        "client": client,
        "storage": PaperStorage(db_path),
        "service": VaultWriteService(vault),
        "cfg": cfg,
    }


def test_p0_1_manifest_ignores_undeclared_direct_files(env):
    """P0-1: 目录有合法 manifest 时，未声明的 direct 文件绝不能成为已绑定来源."""
    folder = env["papers"] / "方向A" / "ManifestAuthorityPaper"
    folder.mkdir(parents=True)
    (folder / "bound.md").write_text("# Bound Document\n", encoding="utf-8")
    (folder / "extra.md").write_text("# Unbound Extra Document\n", encoding="utf-8")

    pid = new_paper_id()
    sid = new_source_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid,
                "role": "TRANSLATION_FULL",
                "path": "bound.md",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper(pid)
    assert paper is not None

    sources = storage.list_sources(pid, include_inactive=True)
    paths = {s.rel_path for s in sources}
    assert "bound.md" in paths
    # extra.md must NOT be bound as a paper source
    assert "extra.md" not in paths


def test_p0_2_manifest_primary_false_not_heuristically_elevated(env):
    """P0-2: Manifest 明确 primary=false 的单 PDF 绝不能被启发式自动提升为 true."""
    folder = env["papers"] / "方向A" / "PrimaryFalsePaper"
    folder.mkdir(parents=True)
    (folder / "paper.pdf").write_bytes(PDF_SAMPLE)

    pid = new_paper_id()
    sid = new_source_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid,
                "role": "ORIGINAL_PDF",
                "path": "paper.pdf",
                "primary": False,  # Explicitly False!
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    sources = storage.list_sources(pid)
    assert len(sources) == 1
    # Must NOT be elevated to True
    assert sources[0].is_primary is False

    paper = storage.get_paper(pid)
    assert paper.primary_pdf_source_id is None


def test_p0_3_role_media_mismatch_becomes_degraded(env):
    """P0-3: Manifest schema-valid 但 role 与物理文件不匹配必须标记为 DEGRADED."""
    folder = env["papers"] / "方向B" / "RoleMismatchPaper"
    folder.mkdir(parents=True)
    (folder / "fake.md").write_text("# Markdown file pretending to be PDF\n", encoding="utf-8")

    pid = new_paper_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": new_source_id(),
                "role": "ORIGINAL_PDF",  # Declared as PDF, but is a .md file!
                "path": "fake.md",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    res = discover_papers(ScanConfig(root=env["papers"]))
    assert len(res.papers) == 1
    assert res.papers[0].binding_state == BindingState.DEGRADED


def test_p0_4_manifest_ancestor_symlink_rejected_and_degraded(env):
    """P0-4: Manifest source path 的 symlink 祖先或目标必须在 scanner 侧被识别并降级."""
    folder = env["papers"] / "方向C" / "SymlinkPaper"
    folder.mkdir(parents=True)
    real_dir = folder / "real_dir"
    real_dir.mkdir()
    (real_dir / "target.md").write_text("# Content\n", encoding="utf-8")

    alias_dir = folder / "alias_dir"
    try:
        alias_dir.symlink_to("real_dir")
    except OSError:
        pytest.skip("Symlink creation not supported on this platform")

    pid = new_paper_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": new_source_id(),
                "role": "TRANSLATION_FULL",
                "path": "alias_dir/target.md",  # Ancestor alias_dir is a symlink!
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    res = discover_papers(ScanConfig(root=env["papers"]))
    assert len(res.papers) == 1
    # Must be DEGRADED, not ADOPTED
    assert res.papers[0].binding_state == BindingState.DEGRADED


def test_p0_5_resolve_crash_recovery_handler_rolls_forward(env):
    """P0-5: Resolve 崩溃恢复 handler 真正 roll-forward 并提交 intent."""
    folder = env["papers"] / "方向D" / "ResolveCrashRecoveryPaper"
    folder.mkdir(parents=True)
    (folder / "A.md").write_text("# Doc A\n", encoding="utf-8")

    pid = new_paper_id()
    sid = new_source_id()

    # Pre-index paper in AMBIGUOUS state
    storage = PaperStorage(env["db"])
    paper = Paper(
        paper_id=pid,
        folder_relpath="方向D/ResolveCrashRecoveryPaper",
        display_title="ResolveCrashRecoveryPaper",
        binding_state=BindingState.AMBIGUOUS,
    )
    storage.upsert_paper(paper, allow_folder_move=True)

    # Simulate: manifest was published to disk before crash
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid,
                "role": "TRANSLATION_FULL",
                "path": "A.md",
                "primary": True,
                "active": True,
            }
        ],
        "title_override": "Crash Recovered Title",
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Simulate: pending intent left in DB
    intent_id = storage.begin_write_intent(
        pid,
        "resolve",
        {"folder_relpath": paper.folder_relpath, "papers_root_rel": "论文"},
    )

    # Run recovery
    report = recover_pending_writes(storage, env["service"])
    assert report.resolved == 1
    assert report.outcomes[0].operation == "resolve"
    assert report.outcomes[0].action == "completed"

    # Verify SQLite was rolled forward to ADOPTED
    recovered = storage.get_paper(pid)
    assert recovered.binding_state == BindingState.ADOPTED
    assert recovered.title_override == "Crash Recovered Title"


def test_p0_6_idempotent_roll_forward_restores_canonical_manifest_fields(env):
    """P0-6: 预写 manifest 幂等恢复时，完整回填 note_id, tags, title_override."""
    folder = env["papers"] / "方向E" / "IdempotentCanonicalPaper"
    folder.mkdir(parents=True)
    (folder / "doc.md").write_text("# Doc\n", encoding="utf-8")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper_by_folder("方向E/IdempotentCanonicalPaper")
    pid = paper.paper_id

    # Pre-write manifest on disk with full metadata
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "title_override": "Canonical Title",
        "tags": ["AI", "Research"],
        "note": {"note_id": "note_abcd1234-1111-4000-8000-000000000000", "path": "notes.md"},
        "sources": [
            {
                "source_id": new_source_id(),
                "role": "TRANSLATION_FULL",
                "path": "doc.md",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    client = env["client"]
    payload = {
        "sources": [
            {"rel_path": "doc.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
        ]
    }
    res = client.post(f"/api/paper/papers/{pid}/resolve", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["binding_state"] == "ADOPTED"
    assert data["title_override"] == "Canonical Title"
    assert data["note_id"] == "note_abcd1234-1111-4000-8000-000000000000"
    assert data["paper_tags"] == ["AI", "Research"]

    # Verify SQLite row matches
    db_paper = storage.get_paper(pid)
    assert db_paper.binding_state == BindingState.ADOPTED
    assert db_paper.title_override == "Canonical Title"
    assert db_paper.note_id == "note_abcd1234-1111-4000-8000-000000000000"
    assert db_paper.paper_tags == ["AI", "Research"]


def test_p0_7_resolve_title_override_single_authority(env):
    """P0-7: resolve 传入的 title_override 写入 Manifest 权威，绝不发生双写分裂."""
    folder = env["papers"] / "方向F" / "SingleAuthorityTitlePaper"
    folder.mkdir(parents=True)
    (folder / "A.md").write_text("# Doc\n", encoding="utf-8")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper_by_folder("方向F/SingleAuthorityTitlePaper")
    pid = paper.paper_id

    client = env["client"]
    res = client.post(
        f"/api/paper/papers/{pid}/resolve",
        json={
            "sources": [
                {"rel_path": "A.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
            ],
            "title_override": "Authoritative Title Override",
        },
    )
    assert res.status_code == 200
    assert res.json()["title_override"] == "Authoritative Title Override"

    # Verify manifest on disk carries the exact same title_override
    manifest = json.loads((folder / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["title_override"] == "Authoritative Title Override"
