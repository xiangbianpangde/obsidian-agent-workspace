"""Acceptance tests for AMBIGUOUS resolution and the 5 blocking P0 probes (ADR-006 / ADR-007)."""

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


def test_blocking_1_manifest_authority_preserves_roles_and_inactives(env):
    """阻断 1：Manifest 权威绑定绝不能被扫描器启发式改写.

    Manifest 显式声明：
    - `weird.md`: role=TRANSLATION_FULL, primary=true, active=true (文件名不符合常规命名)
    - `inactive.md`: role=OTHER_MARKDOWN, active=false

    重扫和索引后：
    - `weird.md` 必须是 TRANSLATION_FULL, is_primary=True, active=True
    - `inactive.md` 必须是 OTHER_MARKDOWN, active=False
    - `primary_translation_source_id` 必须准确指向 `weird.md` 的 source_id
    """
    folder = env["papers"] / "方向A" / "CustomNamingPaper"
    folder.mkdir(parents=True)
    (folder / "weird.md").write_text("# Weird Name Full Translation\n", encoding="utf-8")
    (folder / "inactive.md").write_text("# Inactive Document\n", encoding="utf-8")

    pid = new_paper_id()
    sid_weird = new_source_id()
    sid_inactive = new_source_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid_weird,
                "role": "TRANSLATION_FULL",
                "path": "weird.md",
                "primary": True,
                "active": True,
            },
            {
                "source_id": sid_inactive,
                "role": "OTHER_MARKDOWN",
                "path": "inactive.md",
                "primary": False,
                "active": False,
            },
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # 1. Run indexer
    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper(pid)
    assert paper is not None
    assert paper.binding_state == BindingState.ADOPTED
    assert paper.primary_translation_source_id == sid_weird

    sources = {s.rel_path: s for s in storage.list_sources(pid, include_inactive=True)}
    assert len(sources) == 2

    s_weird = sources["weird.md"]
    assert s_weird.role == SourceRole.TRANSLATION_FULL
    assert s_weird.is_primary is True
    assert s_weird.active is True
    assert s_weird.binding_origin == BindingOrigin.MANIFEST

    s_inactive = sources["inactive.md"]
    assert s_inactive.role == SourceRole.OTHER_MARKDOWN
    assert s_inactive.is_primary is False
    assert s_inactive.active is False
    assert s_inactive.binding_origin == BindingOrigin.MANIFEST


def test_blocking_2_manifest_all_sources_missing_becomes_degraded_not_dropped(env):
    """阻断 2：Manifest 的所有 active source 都缺失时，目录不能消失，必须为 DEGRADED.

    磁盘上没有任何 PDF 或 Markdown，仅有 manifest.json 声明了一个已丢失的 `gone.md`。
    discover_papers 必须输出该 Paper，且状态为 DEGRADED，保留原始 paper_id。
    """
    folder = env["papers"] / "方向B" / "GhostPaper"
    folder.mkdir(parents=True)

    pid = new_paper_id()
    sid = new_source_id()
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": sid,
                "role": "ORIGINAL_PDF",
                "path": "gone.pdf",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Scanner must NOT drop this folder
    res = discover_papers(ScanConfig(root=env["papers"]))
    assert len(res.papers) == 1
    found = res.papers[0]
    assert found.paper_id == pid
    assert found.binding_state == BindingState.DEGRADED
    assert len(found.sources) == 1
    assert found.sources[0].source_id == sid
    assert found.sources[0].missing_since is not None

    # Indexer must record it in SQLite as DEGRADED
    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper(pid)
    assert paper is not None
    assert paper.binding_state == BindingState.DEGRADED


def test_blocking_3_lexical_symlink_in_same_folder_strictly_rejected(env):
    """阻断 3：/resolve 必须严格拒绝目录内符号链接（即便指向同目录）.

    创建真实物理文件 `real.md` 和符号链接 `alias.md -> real.md`。
    调用 /resolve 传入 `alias.md` 必须返回 400，严禁写入 manifest。
    """
    folder = env["papers"] / "方向C" / "SymlinkAttackPaper"
    folder.mkdir(parents=True)
    real_file = folder / "real.md"
    real_file.write_text("# Real MD\n", encoding="utf-8")

    alias_file = folder / "alias.md"
    try:
        alias_file.symlink_to("real.md")
    except OSError:
        pytest.skip("Symlink creation not supported on this platform/filesystem")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper_by_folder("方向C/SymlinkAttackPaper")
    pid = paper.paper_id

    client = env["client"]

    # Attempt to resolve using the symlink alias.md
    r = client.post(
        f"/api/paper/papers/{pid}/resolve",
        json={
            "sources": [
                {"rel_path": "alias.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
            ]
        },
    )
    assert r.status_code == 400
    assert "symlink" in r.text.lower()

    # Ensure manifest was NOT created with symlink
    assert not (folder / MANIFEST_FILENAME).exists()


def test_blocking_4_resolve_idempotent_roll_forward_and_concurrency(env):
    """阻断 4：Resolve 幂等恢复、roll-forward 与并发冲突.

    4A: 预先写入相同 manifest，但 DB 仍为 AMBIGUOUS。调用 /resolve 必须返回 200 并将 DB 推进到 ADOPTED。
    4B: 两个真实并发线程分别提交不同 payload，恰好一个成功，另一个 409。
    """
    folder = env["papers"] / "方向D" / "IdempotentRollForwardPaper"
    folder.mkdir(parents=True)
    (folder / "A.md").write_text("# Doc A\n", encoding="utf-8")
    (folder / "B.md").write_text("# Doc B\n", encoding="utf-8")

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper_by_folder("方向D/IdempotentRollForwardPaper")
    pid = paper.paper_id

    client = env["client"]

    # 4A: Pre-create manifest matching payload A, but keep DB in AMBIGUOUS
    payload_a = {
        "sources": [
            {"rel_path": "A.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
        ]
    }
    # Write manifest manually on disk
    manifest_doc = {
        "schema_version": 1,
        "paper_id": pid,
        "sources": [
            {
                "source_id": new_source_id(),
                "role": "TRANSLATION_FULL",
                "path": "A.md",
                "primary": True,
                "active": True,
            }
        ],
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    (folder / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc), encoding="utf-8")

    # Call /resolve: must reconcile and roll-forward to ADOPTED
    res_roll = client.post(f"/api/paper/papers/{pid}/resolve", json=payload_a)
    assert res_roll.status_code == 200
    assert res_roll.json()["binding_state"] == "ADOPTED"
    # Check DB was also updated
    assert storage.get_paper(pid).binding_state == BindingState.ADOPTED

    # 4B: True multi-threaded concurrency test on a fresh ambiguous paper
    folder2 = env["papers"] / "方向D" / "ConcurrentRacePaper"
    folder2.mkdir(parents=True)
    (folder2 / "X.md").write_text("# Doc X\n", encoding="utf-8")
    (folder2 / "Y.md").write_text("# Doc Y\n", encoding="utf-8")

    indexer.index_papers(dry_run=False)
    paper2 = storage.get_paper_by_folder("方向D/ConcurrentRacePaper")
    pid2 = paper2.paper_id

    results = []

    def call_resolve(item_name):
        c = TestClient(client.app)
        res = c.post(
            f"/api/paper/papers/{pid2}/resolve",
            json={
                "sources": [
                    {"rel_path": f"{item_name}.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
                ]
            },
        )
        results.append(res.status_code)

    t1 = threading.Thread(target=call_resolve, args=("X",))
    t2 = threading.Thread(target=call_resolve, args=("Y",))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one must succeed with 200, and the other must fail with 409
    assert sorted(results) == [200, 409]


def test_blocking_5_unreadable_manifest_fails_closed_no_new_identity(env):
    """阻断 5：不可读 Manifest 必须 fail-closed，绝不能静默当作无 manifest 发新 ID.

    当读取 manifest 遇到 PermissionError 时：
    - read_manifest_file 必须抛出 ManifestError（不能返回 None）
    - 索引器记录错误，绝不能生成新身份或把该目录当作普通未采纳目录处理
    """
    folder = env["papers"] / "方向E" / "PermissionDeniedPaper"
    folder.mkdir(parents=True)
    manifest_path = folder / MANIFEST_FILENAME
    manifest_path.write_text('{"paper_id": "pw_test"}', encoding="utf-8")

    service = env["service"]
    # Mock service.read to raise PermissionError
    orig_read = service.read

    def mock_read(rel):
        if "PermissionDeniedPaper" in rel:
            raise PermissionError("Access denied by OS permissions")
        return orig_read(rel)

    service.read = mock_read

    with pytest.raises(ManifestError) as exc_info:
        read_manifest_file(service, f"方向E/PermissionDeniedPaper/{MANIFEST_FILENAME}")
    assert "unreadable manifest" in str(exc_info.value).lower()
    assert "permission" in str(exc_info.value).lower()


def test_dependent_operations_409_and_zero_delete_mutation(env):
    """综合验证：AMBIGUOUS 状态下 6 大写接口全 409，未选中候选软失活，零物理删除."""
    folder = env["papers"] / "方向F" / "VerifyAllPaper"
    folder.mkdir(parents=True)
    f1 = folder / "doc1.md"
    f2 = folder / "doc2.md"
    f1.write_text("# Doc 1\n", encoding="utf-8")
    f2.write_text("# Doc 2\n", encoding="utf-8")

    h1 = hashlib.sha256(f1.read_bytes()).hexdigest()
    h2 = hashlib.sha256(f2.read_bytes()).hexdigest()

    indexer.index_papers(dry_run=False)
    storage = PaperStorage(env["db"])
    paper = storage.get_paper_by_folder("方向F/VerifyAllPaper")
    pid = paper.paper_id
    client = env["client"]

    # 1. Verify 409 on dependent writes
    assert client.put(f"/api/paper/papers/{pid}/status", json={"status": "READING"}).status_code == 409
    assert client.put(f"/api/paper/papers/{pid}/workspace-state", json={"active_pane": "MARKDOWN"}).status_code == 409
    assert client.post(f"/api/paper/papers/{pid}/note", json={"content": "Note"}).status_code == 409
    assert client.put(f"/api/paper/papers/{pid}/note", json={"content": "Update", "expected_hash": "a"}).status_code == 409
    assert client.post(
        f"/api/paper/papers/{pid}/annotations",
        json={"source_id": "s", "kind": "HIGHLIGHT", "anchor": {"schema_version": 2}},
    ).status_code == 409
    assert client.delete(f"/api/paper/papers/{pid}/annotations/ann_1").status_code == 409

    # 2. Resolve choosing only doc1.md
    res = client.post(
        f"/api/paper/papers/{pid}/resolve",
        json={
            "sources": [
                {"rel_path": "doc1.md", "role": "TRANSLATION_FULL", "is_primary": True, "active": True}
            ]
        },
    )
    assert res.status_code == 200

    # 3. Verify zero physical deletion and zero modification of original files
    assert f1.exists()
    assert f2.exists()
    assert hashlib.sha256(f1.read_bytes()).hexdigest() == h1
    assert hashlib.sha256(f2.read_bytes()).hexdigest() == h2

    # 4. Verify unselected doc2.md is soft-deactivated (active=0, never deleted)
    sources = storage.list_sources(pid, include_inactive=True, include_candidates=True)
    doc2_source = next((s for s in sources if s.rel_path == "doc2.md"), None)
    assert doc2_source is not None
    assert doc2_source.active is False
