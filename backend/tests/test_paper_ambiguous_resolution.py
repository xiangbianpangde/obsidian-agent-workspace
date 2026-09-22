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
    new_note_id,
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


def test_p0_a_degraded_manifest_not_elevated_by_idempotent_resolve(env):
    """P0-A: 已有 DEGRADED Manifest 决不能被幂等 resolve 错误提升为 ADOPTED."""
    folder = env["papers"] / "方向G" / "DegradedResolvePaper"
    folder.mkdir(parents=True)
    # gone.md does NOT exist on disk!

    pid = new_paper_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": new_source_id(),
                "role": "TRANSLATION_FULL",
                "path": "gone.md",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Index: must be DEGRADED
    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper(pid)
    assert paper.binding_state == BindingState.DEGRADED

    # Call /resolve with matching payload: must NOT become ADOPTED!
    client = env["client"]
    res = client.post(
        f"/api/paper/papers/{pid}/resolve",
        json={
            "sources": [
                {"rel_path": "gone.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
            ]
        },
    )
    assert res.status_code == 200
    # Must remain DEGRADED because gone.md is still missing on disk!
    assert res.json()["binding_state"] == "DEGRADED"
    assert storage.get_paper(pid).binding_state == BindingState.DEGRADED


def test_p0_c_manifest_symlink_fails_closed(env):
    """P0-C: Manifest 自身的符号链接必须被 scanner 和 resolve 严格拒绝."""
    folder = env["papers"] / "方向H" / "ManifestSymlinkPaper"
    folder.mkdir(parents=True)
    (folder / "paper.pdf").write_bytes(PDF_SAMPLE)

    ext_manifest = env["vault"] / "external_manifest.json"
    ext_manifest.write_text('{"paper_id": "pw_external_hack"}', encoding="utf-8")

    manifest_file = folder / MANIFEST_FILENAME
    try:
        manifest_file.symlink_to(ext_manifest)
    except OSError:
        pytest.skip("Symlink not supported")

    # Scanner must fail closed and record error
    res = discover_papers(ScanConfig(root=env["papers"]))
    assert any("is a symlink" in err for err in res.errors)
    assert not any(p.folder_relpath.endswith("ManifestSymlinkPaper") for p in res.papers)


def test_p0_d_legacy_unbound_sources_retired_when_manifest_present(env):
    """P0-D: 历史 SQLite 中的未声明 active non-candidate 来源重扫后必须退休."""
    folder = env["papers"] / "方向I" / "LegacyRetirePaper"
    folder.mkdir(parents=True)
    (folder / "bound.md").write_text("# Bound\n", encoding="utf-8")
    (folder / "legacy_extra.md").write_text("# Extra\n", encoding="utf-8")

    pid = new_paper_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": new_source_id(),
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

    # Simulate legacy state in DB: legacy_extra.md was previously marked active=1
    storage = PaperStorage(env["db"])
    paper = Paper(paper_id=pid, folder_relpath="方向I/LegacyRetirePaper", display_title="Legacy")
    storage.upsert_paper(paper, allow_folder_move=True)
    extra_sid = new_source_id()
    storage.upsert_source(
        PaperSource(
            source_id=extra_sid,
            paper_id=pid,
            role=SourceRole.OTHER_MARKDOWN,
            rel_path="legacy_extra.md",
            active=True,
            is_candidate=False,
        )
    )

    # Re-index: bound.md must be active, and legacy_extra.md must be retired (active=0)!
    indexer.index_papers(dry_run=False)

    sources = {s.rel_path: s for s in storage.list_sources(pid, include_inactive=True)}
    assert sources["bound.md"].active is True
    assert sources["legacy_extra.md"].active is False


def test_p0_e_cas_failure_rolls_back_concurrent_resolve(env):
    """P0-E: 跨进程并发冲突时 CAS 严格拒绝并回滚事务."""
    storage = PaperStorage(env["db"])
    pid = new_paper_id()
    paper = Paper(
        paper_id=pid,
        folder_relpath="方向J/CASPaper",
        display_title="CAS",
        binding_state=BindingState.RESOLVED,  # Concurrently moved away from AMBIGUOUS!
    )
    storage.upsert_paper(paper, allow_folder_move=True)

    with pytest.raises(Exception) as exc_info:
        storage.commit_resolved_adoption(
            pid,
            sources=[],
            binding_state=BindingState.ADOPTED,
            expected_state=BindingState.AMBIGUOUS,
        )
    assert "cas failure" in str(exc_info.value).lower()


def test_p0_f_rename_recovery_preserves_custom_note_path_and_metadata(env):
    """P0-F: rename recovery 更新 manifest 时必须保留 custom-note.md 路径与元数据."""
    folder = env["papers"] / "方向K" / "CustomNoteRenamePaper"
    folder.mkdir(parents=True)
    (folder / "paper.pdf").write_bytes(PDF_SAMPLE)
    (folder / "my-custom-note.md").write_text("# Custom Note\n", encoding="utf-8")

    pid = new_paper_id()
    sid = new_source_id()
    nid = new_note_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "title_override": "Custom Title",
        "tags": ["AI"],
        "note": {"note_id": nid, "path": "my-custom-note.md"},
        "sources": [
            {
                "source_id": sid,
                "role": "ORIGINAL_PDF",
                "path": "paper.pdf",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Initial index
    indexer.index_papers(dry_run=False)

    # Rename paper.pdf -> renamed.pdf on disk
    (folder / "paper.pdf").rename(folder / "renamed.pdf")

    # Reindex (triggers rename recovery)
    indexer.index_papers(dry_run=False)

    # Check manifest on disk: note.path MUST be my-custom-note.md, NOT notes.md!
    mf_after = json.loads((folder / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert mf_after["note"]["path"] == "my-custom-note.md"
    assert mf_after["note"]["note_id"] == nid
    assert mf_after["title_override"] == "Custom Title"
    assert mf_after["tags"] == ["AI"]

    # Rebuild database from scratch and verify note binding recovered
    env["storage"].close()
    env["db"].unlink()
    for p in (Path(str(env["db"]) + "-wal"), Path(str(env["db"]) + "-shm")):
        p.unlink(missing_ok=True)
    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    p_rebuilt = storage.get_paper(pid)
    assert p_rebuilt is not None
    assert p_rebuilt.note_id == nid


def test_p0_h_ensure_adopted_race_conflicting_payload_rejected(env):
    """P0-H: 并发采纳竞争中，若胜者 manifest 与当前请求 payload 冲突必须抛出 ConflictError."""
    from backend.app.paper.writer import ConflictError

    folder = env["papers"] / "方向L" / "RaceConflictPaper"
    folder.mkdir(parents=True)
    (folder / "A.md").write_text("# A\n", encoding="utf-8")
    (folder / "B.md").write_text("# B\n", encoding="utf-8")

    pid = new_paper_id()
    storage = PaperStorage(env["db"])
    service = env["service"]

    # Winner writes manifest with A.md
    winner_paper = Paper(paper_id=pid, folder_relpath="方向L/RaceConflictPaper")
    winner_sources = [
        PaperSource(
            source_id=new_source_id(),
            paper_id=pid,
            role=SourceRole.TRANSLATION_FULL,
            rel_path="A.md",
            is_primary=True,
        )
    ]
    ensure_adopted(storage, service, winner_paper, winner_sources, operation="manual_binding", papers_root_rel="论文")

    # Loser attempts ensure_adopted with B.md (conflicting payload)
    loser_paper = Paper(paper_id=pid, folder_relpath="方向L/RaceConflictPaper")
    loser_sources = [
        PaperSource(
            source_id=new_source_id(),
            paper_id=pid,
            role=SourceRole.TRANSLATION_FULL,
            rel_path="B.md",
            is_primary=True,
        )
    ]
    with pytest.raises(ConflictError) as exc_info:
        ensure_adopted(storage, service, loser_paper, loser_sources, operation="manual_binding", papers_root_rel="论文")
    assert "winning manifest has different sources or title" in str(exc_info.value)


def test_reviewer_1_update_manifest_failure_fault_injection(env, monkeypatch):
    """P0-G 故障注入：若 update_manifest 失败，SQLite 绝不同步，且错误被报告."""
    folder = env["papers"] / "方向M" / "FaultInjectRenamePaper"
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
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Initial index
    indexer.index_papers(dry_run=False)

    # Rename paper.pdf -> renamed.pdf
    (folder / "paper.pdf").rename(folder / "renamed.pdf")

    # Fault injection: make update_manifest raise IOError
    def mock_fail_update(*args, **kwargs):
        raise IOError("Injected disk error during manifest update")

    import backend.scripts.paper_index as index_mod
    monkeypatch.setattr(index_mod, "update_manifest", mock_fail_update)

    rep = index_mod.index_papers(dry_run=False)
    # 1. Error must be reported in errors list
    assert any("Injected disk error" in err for err in rep["errors"])

    # 2. SQLite must NOT be updated with renamed.pdf as active!
    storage = PaperStorage(env["db"])
    active_sources = [s for s in storage.list_sources(pid) if s.active]
    active_paths = {s.rel_path for s in active_sources}
    assert "renamed.pdf" not in active_paths, "renamed.pdf must not be active in SQLite when manifest update failed!"


def test_reviewer_2_manifest_authority_over_stale_sqlite_tags(env):
    """ADR-007: 重扫时 Manifest 权威标签必须覆盖 SQLite 的陈旧标签."""
    folder = env["papers"] / "方向N" / "StaleSqliteTagsPaper"
    folder.mkdir(parents=True)
    (folder / "doc.md").write_text("# Doc\n", encoding="utf-8")

    pid = new_paper_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "tags": ["ManifestTagA", "ManifestTagB"],  # Authoritative tags in manifest!
        "title_override": "ManifestTitle",
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

    # Pre-seed stale row in SQLite
    storage = PaperStorage(env["db"])
    paper = Paper(
        paper_id=pid,
        folder_relpath="方向N/StaleSqliteTagsPaper",
        display_title="StaleTitle",
        title_override="OldSqliteTitle",
        paper_tags=["OldSqliteTag"],
    )
    storage.upsert_paper(paper, allow_folder_move=True)

    # Reindex: Manifest authority must prevail!
    indexer.index_papers(dry_run=False)

    updated = storage.get_paper(pid)
    assert updated.paper_tags == ["ManifestTagA", "ManifestTagB"], "Manifest tags must overwrite stale SQLite tags!"
    assert updated.title_override == "ManifestTitle", "Manifest title_override must overwrite stale SQLite title!"


def test_reviewer_3_resolve_race_conflicting_title_returns_409(env):
    """P0-H: /resolve 竞争中胜者 title_override 冲突时，必须返回 HTTP 409（而不是 500）."""
    folder = env["papers"] / "方向O" / "ResolveTitleRacePaper"
    folder.mkdir(parents=True)
    (folder / "doc.md").write_text("# Doc\n", encoding="utf-8")

    pid = new_paper_id()
    sid = new_source_id()
    # Winner pre-creates manifest with Title Winner
    winner_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "title_override": "TitleWinner",
        "sources": [
            {
                "source_id": sid,
                "role": "TRANSLATION_FULL",
                "path": "doc.md",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(winner_doc), encoding="utf-8")

    storage = PaperStorage(env["db"])
    paper = Paper(
        paper_id=pid,
        folder_relpath="方向O/ResolveTitleRacePaper",
        display_title="Doc",
        binding_state=BindingState.AMBIGUOUS,
    )
    storage.upsert_paper(paper, allow_folder_move=True)

    client = env["client"]
    # Loser attempts resolve with conflicting title
    res = client.post(
        f"/api/paper/papers/{pid}/resolve",
        json={
            "sources": [
                {"rel_path": "doc.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
            ],
            "title_override": "TitleLoser",  # Different title!
        },
    )
    # Must return 409 (not 500!)
    assert res.status_code == 409
    assert "conflict" in res.text.lower()


def test_p0_i_source_id_and_version_stability_across_rename(env):
    """P0-I: rename recovery 原地迁移 source_id，保证旧 ID 稳定继承与版本递增."""
    folder = env["papers"] / "方向P" / "IdentityStabilityPaper"
    folder.mkdir(parents=True)
    (folder / "old.pdf").write_bytes(PDF_SAMPLE)

    pid = new_paper_id()
    sid_old = new_source_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid_old,
                "role": "ORIGINAL_PDF",
                "path": "old.pdf",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Initial index
    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    src_before = storage.get_source(sid_old)
    assert src_before.rel_path == "old.pdf"
    version_before = src_before.source_version

    # Rename old.pdf -> renamed.pdf
    (folder / "old.pdf").rename(folder / "renamed.pdf")

    # Re-index
    indexer.index_papers(dry_run=False)

    # 1. Verify renamed.pdf has the EXACT SAME source_id!
    src_after = storage.get_source(sid_old)
    assert src_after is not None, "source_id must not be lost or changed on rename!"
    assert src_after.rel_path == "renamed.pdf"
    assert src_after.active is True
    # A rename that leaves the bytes untouched is the SAME revision, so the
    # version is preserved: existing annotations still address these bytes.
    # P0-P only requires a bump when the content actually changed.
    assert src_after.source_version == version_before

    # 2. Verify Manifest on disk records the exact same source_id!
    mf = json.loads((folder / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    renamed_entry = next(s for s in mf["sources"] if s["path"] == "renamed.pdf")
    assert renamed_entry["source_id"] == sid_old

    # 3. Rebuild database from scratch: verify source_id remains exact sid_old
    env["storage"].close()
    env["db"].unlink()
    indexer.index_papers(dry_run=False)
    storage_rebuilt = PaperStorage(env["db"])
    src_rebuilt = storage_rebuilt.get_source(sid_old)
    assert src_rebuilt is not None
    assert src_rebuilt.rel_path == "renamed.pdf"


def test_p0_k_concurrent_identical_payload_adopts_winner_source_id(env):
    """P0-K: 两个并发请求提交相同 sources 时，后者必须继承胜者的 canonical source_id."""
    folder = env["papers"] / "方向Q" / "WinnerSourceIdPaper"
    folder.mkdir(parents=True)
    (folder / "doc.md").write_text("# Doc\n", encoding="utf-8")

    pid = new_paper_id()
    sid_winner = new_source_id()
    # Winner created manifest with sid_winner
    winner_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid_winner,
                "role": "TRANSLATION_FULL",
                "path": "doc.md",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(winner_doc), encoding="utf-8")

    # Loser calls ensure_adopted with same path, but different in-memory source_id
    storage = PaperStorage(env["db"])
    service = env["service"]
    loser_paper = Paper(paper_id=pid, folder_relpath="方向Q/WinnerSourceIdPaper")
    loser_sid = new_source_id()
    loser_source = PaperSource(
        source_id=loser_sid,  # Different local ID
        paper_id=pid,
        role=SourceRole.TRANSLATION_FULL,
        rel_path="doc.md",
        is_primary=True,
    )

    # ensure_adopted should adopt winner's source_id
    adopted_paper = ensure_adopted(
        storage,
        service,
        loser_paper,
        [loser_source],
        operation="manual_binding",
        papers_root_rel="论文",
    )
    assert adopted_paper.manifest_relpath == MANIFEST_FILENAME
    # loser_source must have been updated to winner's canonical source_id!
    assert loser_source.source_id == sid_winner


def test_p0_m_rename_source_crash_recovery_handler_real_execution(env):
    """P0-M & P0-N: _recover_rename_source 真实故障注入自愈并推进 Paper 聚合状态."""
    folder = env["papers"] / "方向R" / "RenameCrashRecoveryPaper"
    folder.mkdir(parents=True)
    (folder / "renamed.pdf").write_bytes(PDF_SAMPLE)

    pid = new_paper_id()
    sid = new_source_id()

    # Pre-condition: Manifest on disk was already published with renamed.pdf
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid,
                "role": "ORIGINAL_PDF",
                "path": "renamed.pdf",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # DB state before recovery: Paper is DEGRADED, only has old.pdf (active=1)
    storage = PaperStorage(env["db"])
    paper = Paper(
        paper_id=pid,
        folder_relpath="方向R/RenameCrashRecoveryPaper",
        display_title="RenameCrashRecoveryPaper",
        binding_state=BindingState.DEGRADED,
    )
    storage.upsert_paper(paper, allow_folder_move=True)
    storage.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="old.pdf",
            active=True,
        )
    )

    # Crash left a pending rename_source intent
    intent_id = storage.begin_write_intent(
        pid,
        "rename_source",
        {
            "old_path": "old.pdf",
            "new_path": "renamed.pdf",
            "source_id": sid,
            "folder_relpath": paper.folder_relpath,
            "papers_root_rel": "论文",
        },
    )

    # Execute recovery
    report = recover_pending_writes(storage, env["service"])
    assert report.resolved == 1
    assert report.outcomes[0].operation == "rename_source"
    assert report.outcomes[0].action == "completed"

    # Verify SQLite was rolled forward completely
    recovered_paper = storage.get_paper(pid)
    assert recovered_paper.binding_state == BindingState.ADOPTED
    assert recovered_paper.primary_pdf_source_id == sid

    sources = {s.rel_path: s for s in storage.list_sources(pid, include_inactive=True)}
    assert sources["renamed.pdf"].active is True
    assert sources["renamed.pdf"].source_id == sid
    assert sources["old.pdf"].active is False


def test_p0_o_api_race_winner_source_id_preserved(env):
    """P0-O: 通过 HTTP API 验证跨进程/客户端并发 resolve 时，胜者 source_id 被完全保持在 Manifest 与 SQLite."""
    folder = env["papers"] / "方向S" / "ApiRacePaper"
    folder.mkdir(parents=True)
    (folder / "doc.md").write_text("# Doc\n", encoding="utf-8")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper_by_folder("方向S/ApiRacePaper")
    pid = paper.paper_id

    client = env["client"]

    # Client A calls resolve
    res_a = client.post(
        f"/api/paper/papers/{pid}/resolve",
        json={
            "sources": [
                {"rel_path": "doc.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
            ]
        },
    )
    assert res_a.status_code == 200

    # Read the winning source_id from manifest on disk
    mf = json.loads((folder / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    winner_sid = mf["sources"][0]["source_id"]

    # Verify SQLite source_id matches Manifest exactly
    db_sources = storage.list_sources(pid)
    assert db_sources[0].source_id == winner_sid

    # Client B calls resolve with same sources -> idempotent success, must return exact winner_sid
    res_b = client.post(
        f"/api/paper/papers/{pid}/resolve",
        json={
            "sources": [
                {"rel_path": "doc.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
            ]
        },
    )
    assert res_b.status_code == 200
    # Check that SQLite still matches winner_sid
    db_sources_after = storage.list_sources(pid)
    assert db_sources_after[0].source_id == winner_sid


def test_p0_p_version_monotonicity_preseeded_version_seven(env):
    """P0-P: 既有 source_version=7 改名后必须单调保持或自增，绝不能回退为 1 或 2."""
    folder = env["papers"] / "方向T" / "VersionMonotonicityPaper"
    folder.mkdir(parents=True)
    (folder / "old.pdf").write_bytes(PDF_SAMPLE)

    pid = new_paper_id()
    sid = new_source_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid,
                "role": "ORIGINAL_PDF",
                "path": "old.pdf",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Seed SQLite with source_version=7
    storage = PaperStorage(env["db"])
    paper = Paper(paper_id=pid, folder_relpath="方向T/VersionMonotonicityPaper", display_title="T")
    storage.upsert_paper(paper, allow_folder_move=True)
    storage.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="old.pdf",
            source_version=7,  # Preseeded version 7!
            sha256=hashlib.sha256(PDF_SAMPLE).hexdigest(),
            active=True,
        )
    )

    # Rename old.pdf -> renamed.pdf (content unchanged)
    (folder / "old.pdf").rename(folder / "renamed.pdf")

    # Index: rename recovery must keep version=7 because hash is unchanged!
    indexer.index_papers(dry_run=False)

    src_after = storage.get_source(sid)
    assert src_after is not None
    assert src_after.source_version == 7, f"expected version 7, got {src_after.source_version}"

    # Now modify bytes on disk and reindex -> must increment 7 -> 8!
    (folder / "renamed.pdf").write_bytes(PDF_SAMPLE + b"%new-bytes\n")
    indexer.index_papers(dry_run=False)

    src_bumped = storage.get_source(sid)
    assert src_bumped.source_version == 8, f"expected version 8, got {src_bumped.source_version}"


def test_p0_q_normal_rename_advances_paper_state_to_adopted(env):
    """P0-Q: 单次 index 后，成功 rename recovery 的 Paper 聚合状态直接变为 ADOPTED，不残留 DEGRADED."""
    folder = env["papers"] / "方向U" / "StateAdvanceRenamePaper"
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
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Initial index
    indexer.index_papers(dry_run=False)

    # Rename paper.pdf -> paper_renamed.pdf
    (folder / "paper.pdf").rename(folder / "paper_renamed.pdf")

    # Exactly ONE index run!
    indexer.index_papers(dry_run=False)

    storage = PaperStorage(env["db"])
    paper = storage.get_paper(pid)
    # Must be ADOPTED immediately on the first pass!
    assert paper.binding_state == BindingState.ADOPTED
    assert paper.primary_pdf_source_id == sid


def _race_worker(process_name, title, db_str, vault_str, papers_str, pid, q, b):
    """Module-level so the target survives the macOS spawn pickler.

    Two of these run concurrently against one Vault and database, which is the
    only way to exercise the FileExists race: ``_paper_lock`` serialises threads
    inside a single process, so an in-process thread probe never reaches it.
    """
    try:
        import backend.app.state as app_state
        import backend.app.paper.api as pa
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.app.paper.api import router as pr
        from backend.app.paper.storage import PaperStorage
        from backend.app.paper.writer import VaultWriteService

        class _C:
            vault_root = Path(vault_str)
            papers_root = Path(papers_str)
            papers_max_depth = 6

            @property
            def papers_root_or_default(self):
                return self.papers_root

        app_state._state["cfg"] = _C()
        pa._storage = lambda: PaperStorage(Path(db_str))
        pa._service = lambda: VaultWriteService(Path(vault_str))

        app_local = FastAPI()
        app_local.include_router(pr)
        client = TestClient(app_local)

        b.wait(timeout=30)
        res = client.post(
            f"/api/paper/papers/{pid}/resolve",
            json={
                "sources": [
                    {"rel_path": "doc.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
                ],
                "title_override": title,
            },
        )
        q.put((process_name, res.status_code, res.text[:200]))
    except Exception as exc:  # noqa: BLE001
        q.put((process_name, -1, f"{type(exc).__name__}: {exc}"))


def test_p0_r_multiprocessing_race_winner_source_id_preserved(env):
    """P0-R: 真实跨进程并发探针：两个独立进程同时 resolve，胜者 source_id 必须在 Manifest 与 SQLite 中一致."""
    import multiprocessing

    folder = env["papers"] / "方向V" / "MultiprocessRacePaper"
    folder.mkdir(parents=True)
    (folder / "doc.md").write_text("# Multi-Process Doc\n", encoding="utf-8")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper_by_folder("方向V/MultiprocessRacePaper")
    pid = paper.paper_id
    storage.close()

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()

    procs = [
        ctx.Process(
            target=_race_worker,
            args=(
                name,
                title,
                str(env["db"]),
                str(env["vault"]),
                str(env["papers"]),
                pid,
                queue,
                barrier,
            ),
        )
        for name, title in (("ProcA", "TitleA"), ("ProcB", "TitleB"))
    ]

    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    for p in procs:
        if p.is_alive():
            p.terminate()
            pytest.fail("race worker did not finish within 60s")

    results = [queue.get(timeout=10) for _ in procs]
    codes = sorted(r[1] for r in results)

    # Different title_override means the loser must be rejected, not silently
    # committed: exactly one 200 and one 409 — never two 200s, never a 500.
    assert codes == [200, 409], f"expected one winner and one rejection, got {results}"

    # The surviving Manifest and the surviving SQLite row must agree on identity.
    mf = json.loads((folder / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    winner_sid = mf["sources"][0]["source_id"]

    storage = PaperStorage(env["db"])
    db_paper = storage.get_paper(pid)
    assert db_paper.binding_state == BindingState.ADOPTED
    db_sources = storage.list_sources(pid)
    assert db_sources[0].source_id == winner_sid, (
        "SQLite source_id must match the winning Manifest source_id exactly"
    )


def test_p0_s_recovery_digest_cas_rejects_externally_edited_manifest(env):
    """P0-S: 若 Manifest 在 intent 记录后又被外部编辑，recovery 必须 fail-closed，不能盲目提交.

    这里 intent 记录的是本次写入打算发布的文档摘要（published_manifest_digest）。
    探针故意让磁盘上的 Manifest 与它不符，以模拟"写入后又被人手改了"。
    """
    folder = env["papers"] / "方向W" / "DigestCasPaper"
    folder.mkdir(parents=True)
    (folder / "new.pdf").write_bytes(PDF_SAMPLE)

    pid = new_paper_id()
    sid = new_source_id()
    storage = PaperStorage(env["db"])
    storage.upsert_paper(
        Paper(
            paper_id=pid,
            folder_relpath="方向W/DigestCasPaper",
            display_title="DigestCasPaper",
            binding_state=BindingState.DEGRADED,
        ),
        allow_folder_move=True,
    )

    # Intent records the digest of a document that is NOT what ends up on disk.
    unrelated_digest = "1" * 64
    storage.begin_write_intent(
        pid,
        "rename_source",
        {
            "old_path": "old.pdf",
            "new_path": "new.pdf",
            "source_id": sid,
            "published_manifest_digest": unrelated_digest,
            "folder_relpath": "方向W/DigestCasPaper",
            "papers_root_rel": "论文",
        },
    )

    # Meanwhile someone edits the manifest on disk to something else entirely.
    (folder / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paper_id": pid,
                "sources": [
                    {
                        "source_id": sid,
                        "role": "ORIGINAL_PDF",
                        "path": "new.pdf",
                        "primary": True,
                        "active": True,
                    }
                ],
                "created_at": "2026-09-18T00:00:00Z",
                "updated_at": "2026-09-18T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    report = recover_pending_writes(storage, env["service"])

    # Must NOT roll forward: the manifest the intent was written against is gone.
    assert report.resolved == 0
    assert report.unresolved == 1
    assert report.outcomes[0].action == "digest-mismatch"

    # And the intent stays pending so an operator can still see the divergence.
    assert len(storage.list_pending_write_intents()) == 1








def test_p0_t_sentinel_distinguishes_keep_from_clear(env):
    """P0-T: _UNSET 与 None 必须语义分明 —— 省略字段保留旧值，显式 None 清空.

    批量解析时题目：COALESCE 把两者压成一个，于是 Manifest 主动写 null 的字段
    永远清不掉，陈旧值会活过每一次重建。
    """
    from backend.app.paper.storage import _UNSET

    storage = PaperStorage(env["db"])
    pid = new_paper_id()
    storage.upsert_paper(
        Paper(
            paper_id=pid,
            folder_relpath="方向X/SentinelPaper",
            display_title="Sentinel",
            title_override="Original Title",
            paper_tags=["keep-me"],
            note_id="note_aaaa1111-1111-4111-8111-111111111111",
        ),
        allow_folder_move=True,
    )

    # 1. Omitting a field (sentinel) preserves the stored value.
    storage.commit_resolved_adoption(pid, sources=[], binding_state=BindingState.ADOPTED)
    kept = storage.get_paper(pid)
    assert kept.title_override == "Original Title"
    assert kept.paper_tags == ["keep-me"]
    assert kept.note_id == "note_aaaa1111-1111-4111-8111-111111111111"

    # 2. Passing None explicitly clears the field.
    storage.commit_resolved_adoption(
        pid,
        sources=[],
        title_override=None,
        paper_tags=[],
        note_id=None,
        binding_state=BindingState.ADOPTED,
    )
    cleared = storage.get_paper(pid)
    assert cleared.title_override is None, "explicit null must clear the title"
    assert cleared.paper_tags == [], "explicit empty list must clear the tags"
    assert cleared.note_id is None, "explicit null must clear the note binding"


def test_p0_s2_successful_publish_then_crash_rolls_forward(env):
    """P0-S 反向探针：Manifest 已成功发布、SQLite 提交前崩溃，recovery 必须 roll-forward.

    这是 review 指出的真实缺陷：intent 若记录发布【前】的摘要，recovery 拿到的是
    发布【后】的 Manifest，比对必然失败，正常崩溃现场会永远卡在 pending。
    探针走完整 indexer 路径（真实产出 rename_source intent），再模拟崩溃后恢复。
    """
    folder = env["papers"] / "方向Y" / "HappyPathCrashPaper"
    folder.mkdir(parents=True)
    (folder / "paper.pdf").write_bytes(PDF_SAMPLE)

    pid = new_paper_id()
    sid = new_source_id()
    (folder / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paper_id": pid,
                "sources": [
                    {
                        "source_id": sid,
                        "role": "ORIGINAL_PDF",
                        "path": "paper.pdf",
                        "primary": True,
                        "active": True,
                    }
                ],
                "created_at": "2026-09-18T00:00:00Z",
                "updated_at": "2026-09-18T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    indexer.index_papers(dry_run=False)
    (folder / "paper.pdf").rename(folder / "paper_renamed.pdf")

    # Run the rename path but stop before the SQLite commit, exactly where a crash
    # between the two media would leave things. Only the Vault write is allowed.
    # Patched on the CLASS: the indexer builds its own PaperStorage instance.
    storage = PaperStorage(env["db"])
    real_commit = PaperStorage.commit_write_intent

    def crash_before_commit(self, intent_id):
        raise RuntimeError("simulated crash between manifest publish and SQLite commit")

    PaperStorage.commit_write_intent = crash_before_commit
    try:
        indexer.index_papers(dry_run=False)
    except RuntimeError:
        # The process dies here in reality. What survives is what matters: a
        # published manifest plus a still-pending intent.
        pass
    finally:
        PaperStorage.commit_write_intent = real_commit

    # Precondition: the manifest DID get published with the new path, and the
    # intent is still pending — the real crash shape.
    mf = json.loads((folder / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert mf["sources"][0]["path"] == "paper_renamed.pdf"
    pending = storage.list_pending_write_intents()
    rename_pending = [p for p in pending if p["operation"] == "rename_source"]
    assert rename_pending, "the rename intent must survive the crash as pending"

    # The intent must carry the digest of what was published, not of what was replaced.
    payload = json.loads(rename_pending[0]["payload_json"])
    assert payload.get("published_manifest_digest"), (
        "intent must record the published digest, otherwise recovery can never match"
    )

    # Recover: this MUST roll forward, not dead-end on digest-mismatch.
    report = recover_pending_writes(storage, env["service"])
    assert report.resolved == 1, f"normal crash must roll forward, got {report.outcomes}"
    assert report.outcomes[0].action == "completed"

    recovered = storage.get_paper(pid)
    assert recovered.binding_state == BindingState.ADOPTED
    assert recovered.primary_pdf_source_id == sid

    live = {s.rel_path: s for s in storage.list_sources(pid)}
    assert "paper_renamed.pdf" in live
    assert live["paper_renamed.pdf"].source_id == sid
    assert not storage.list_pending_write_intents(), "intent must be committed after recovery"
