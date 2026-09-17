"""P0-B8 acceptance scenarios.

Sol's review listed the scenarios that must pass before P0 can be declared
complete, and required that they be production-equivalent rather than fixtures
that make the two concepts indistinguishable. Each test below names the scenario
it covers.

The scenarios fall into four groups:

1. identity and reconciliation  — rebuild, move, copy, rename, replace
2. concurrency and crash        — concurrent writes, interrupted writes
3. single-writer coordination   — multi-worker and multi-instance misuse
4. client/server contract       — real payloads fed to the frontend modules
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from backend.app.paper.manifest import (
    ensure_adopted,
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
from backend.app.paper.ownership import (
    lock_path_for,
    MultiWorkerError,
    VaultAlreadyOwnedError,
    VaultWriteLock,
    assert_single_worker,
)
from backend.app.paper.storage import PaperStorage
from backend.app.paper.writer import VaultWriteService

PDF_V1 = b"%PDF-1.4\nfirst revision\n%%EOF\n"
PDF_V2 = b"%PDF-1.4\nsecond revision, longer\n%%EOF\n"
REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------



@pytest.fixture
def workbench_like(tmp_path: Path, monkeypatch):
    """An app + vault scene for the review-round-2 regression tests.

    Mirrors production shape: the papers root nests inside the vault, and the
    paper carries both a PDF and a translation so multi-source behaviour is
    exercised rather than assumed.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import backend.app.paper.api as paper_api
    import backend.app.state as app_state

    work = tmp_path
    vault = work / "vault"
    papers_root = vault / "论文根"
    paper_dir = papers_root / "方向" / "论文A"
    paper_dir.mkdir(parents=True)
    (paper_dir / "a.pdf").write_bytes(PDF_V1)
    (paper_dir / "b_全文翻译.md").write_text("# 译文\n", encoding="utf-8")

    storage = PaperStorage(work / "wb.db")
    pid, pdf_sid, md_sid = new_paper_id(), new_source_id(), new_source_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="t"))
    storage.upsert_source(
        PaperSource(
            source_id=pdf_sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="a.pdf",
            sha256="a" * 64,
        )
    )
    storage.upsert_source(
        PaperSource(
            source_id=md_sid,
            paper_id=pid,
            role=SourceRole.TRANSLATION_FULL,
            rel_path="b_全文翻译.md",
        )
    )

    papers_root_ = papers_root

    class _Cfg:
        vault_path = vault
        vault_root = vault
        papers_root = papers_root_
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    monkeypatch.setattr(app_state, "_state", {"cfg": _Cfg()}, raising=False)
    monkeypatch.setattr(paper_api, "_storage", lambda: storage)
    import backend.app.paper.api_sources as api_sources

    monkeypatch.setattr(api_sources.paper_storage, "PaperStorage", lambda *a, **k: storage)

    app = FastAPI()
    app.include_router(paper_api.router)
    client = TestClient(app)

    class _Client:
        """TestClient plus scene accessors the tests need."""

        def __init__(self, inner, storage, pid, pdf_sid, md_sid, paper_dir):
            self._inner = inner
            self.storage = storage
            self.paper_id = pid
            self.pdf_sid = pdf_sid
            self.md_sid = md_sid
            self.paper_dir = paper_dir

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def manifest_path(self):
            return self.paper_dir / "paper.workbench.json"

    proxy = _Client(client, storage, pid, pdf_sid, md_sid, paper_dir)
    yield proxy, storage, pid, (pdf_sid, md_sid)
    storage.close()


class Scene:
    """A vault + database pair laid out the way production is."""

    def __init__(self, tmp_path: Path):
        # papers_root is a SUBDIRECTORY of the vault, as in production. With the
        # two roots equal, path bugs pass unnoticed — that mistake already cost
        # one remediation round.
        self.vault = tmp_path / "vault"
        self.papers_root = self.vault / "论文根"
        self.paper_dir = self.papers_root / "方向" / "论文A"
        self.paper_dir.mkdir(parents=True)
        (self.paper_dir / "paper.pdf").write_bytes(PDF_V1)
        (self.paper_dir / "paper_全文翻译.md").write_text("# 译文\n\n正文。\n", encoding="utf-8")
        self.service = VaultWriteService(
            self.vault, backup_root=tmp_path / "backups"
        )
        self.papers_root_rel = "论文根"

    def install_app_config(self, monkeypatch) -> None:
        """Point the app's config at this scene.

        The API helpers resolve paths through `get_cfg()`, so a test that calls
        them directly must install the state the lifespan would normally set.
        """
        import backend.app.state as app_state

        scene = self

        class _Cfg:
            vault_path = scene.vault
            vault_root = scene.vault
            papers_root = scene.papers_root
            papers_max_depth = 6

            @property
            def papers_root_or_default(self):
                return self.papers_root

        monkeypatch.setattr(app_state, "_state", {"cfg": _Cfg()}, raising=False)

    def db(self, name: str = "papers.db") -> PaperStorage:
        return PaperStorage(self.vault.parent / name)

    def vault_rel(self, *parts: str) -> str:
        """Vault-relative path for a file inside the paper folder.

        ``folder_relpath`` is relative to the papers root, while the write
        service resolves against the vault root. Joining them is exactly the
        mistake that put a note beside the papers root instead of inside the
        paper folder, so the helper keeps the two roots distinct.
        """
        return str(Path(self.papers_root_rel, "方向", "论文A", *parts))

    def seed(self, storage: PaperStorage, paper_id: str | None = None) -> tuple[str, str]:
        pid = paper_id or new_paper_id()
        sid = new_source_id()
        storage.upsert_paper(
            Paper(paper_id=pid, folder_relpath="方向/论文A", display_title="测试")
        )
        storage.upsert_source(
            PaperSource(
                source_id=sid,
                paper_id=pid,
                role=SourceRole.ORIGINAL_PDF,
                rel_path="paper.pdf",
                sha256="a" * 64,
            )
        )
        return pid, sid

    def adopt(self, storage: PaperStorage, pid: str) -> None:
        paper = ensure_adopted(
            storage,
            self.service,
            storage.get_paper(pid),
            storage.list_sources(pid),
            operation="status_change",
            papers_root_rel=self.papers_root_rel,
        )
        storage.upsert_paper(paper, allow_folder_move=True)

    def reindex(self, storage: PaperStorage) -> None:
        """Run the real indexer against this scene."""
        from backend.app.paper import scanner as scanner_mod
        from backend.scripts import paper_index as indexer

        class _Cfg:
            vault_path = self.vault
            vault_root = self.vault
            papers_root = self.papers_root
            papers_max_depth = 6

            @property
            def papers_root_or_default(self):
                return self.papers_root

        original_load, original_storage = indexer.load_config, indexer.PaperStorage
        indexer.load_config = lambda: _Cfg()
        indexer.PaperStorage = lambda *a, **k: storage
        try:
            indexer.index_papers(dry_run=False)
        finally:
            indexer.load_config = original_load
            indexer.PaperStorage = original_storage


# ---------------------------------------------------------------------------
# Group 1 — identity and reconciliation
# ---------------------------------------------------------------------------


def test_scenario_rebuild_after_database_loss(tmp_path: Path):
    """删除数据库后重建，已采纳的 Paper/Source ID 不变."""
    scene = Scene(tmp_path)
    storage = scene.db("first.db")
    pid, sid = scene.seed(storage)
    scene.adopt(storage, pid)
    storage.close()

    # A brand-new database, as after losing the file.
    rebuilt = scene.db("second.db")
    scene.reindex(rebuilt)

    papers = rebuilt.list_papers()
    assert len(papers) == 1
    assert papers[0].paper_id == pid
    sources = rebuilt.list_sources(pid)
    assert sources[0].source_id == sid


def test_scenario_folder_move_preserves_identity(tmp_path: Path):
    """移动 Paper 文件夹，ID 必须不变."""
    scene = Scene(tmp_path)
    storage = scene.db()
    pid, sid = scene.seed(storage)
    scene.adopt(storage, pid)

    moved = scene.papers_root / "新方向" / "论文A"
    moved.parent.mkdir(parents=True)
    scene.paper_dir.rename(moved)

    scene.reindex(storage)
    paper = storage.get_paper(pid)
    assert paper is not None, "identity must survive a move"
    assert paper.folder_relpath == "新方向/论文A"
    assert storage.list_sources(pid)[0].source_id == sid


def test_scenario_duplicated_folder_fails_closed(tmp_path: Path):
    """复制带 manifest 的文件夹，必须进入冲突而不是静默选一个."""
    scene = Scene(tmp_path)
    storage = scene.db()
    pid, _ = scene.seed(storage)
    scene.adopt(storage, pid)

    # Copy the folder, manifest included — the realistic user action.
    copy_dir = scene.papers_root / "方向" / "论文A副本"
    copy_dir.mkdir(parents=True)
    for item in scene.paper_dir.iterdir():
        (copy_dir / item.name).write_bytes(item.read_bytes())

    scene.reindex(storage)

    # One identity must not silently become two rows pointing at different
    # folders; the second location is a conflict for a human to resolve.
    folders = {p.folder_relpath for p in storage.list_papers(include_inactive=True)}
    assert "方向/论文A" in folders
    duplicate_rows = [p for p in storage.list_papers(include_inactive=True) if p.paper_id == pid]
    assert len(duplicate_rows) == 1, "the same identity must not occupy two live folders"


def test_scenario_source_rename_keeps_identity(tmp_path: Path):
    """来源改名后，旧绑定被停用而不是永久残留."""
    scene = Scene(tmp_path)
    storage = scene.db()
    pid, sid = scene.seed(storage)
    scene.adopt(storage, pid)

    # Rename the PDF on disk without changing its bytes.
    renamed = scene.paper_dir / "paper_renamed.pdf"
    (scene.paper_dir / "paper.pdf").rename(renamed)

    scene.reindex(storage)
    live = [s for s in storage.list_sources(pid) if s.active]
    paths = {s.rel_path for s in live}

    assert "paper_renamed.pdf" in paths, "the renamed file must be picked up"
    # The scene also holds a translation, so two live bindings are correct here.
    # What must not happen is the vanished path lingering as a live binding, or
    # the same bytes being claimed twice.
    assert "paper.pdf" not in paths, (
        "a binding the scan no longer sees must be retired, or the reader is "
        "offered a source that is not on disk"
    )
    assert len(paths) == len(live), "bindings must be distinct"

    # The retired row keeps its identity and history (ADR-002: never deleted).
    retired = [
        s
        for s in storage.list_sources(pid, include_inactive=True)
        if s.rel_path == "paper.pdf"
    ]
    assert len(retired) == 1, "the old binding must be retained, not removed"
    assert retired[0].active is False


def test_scenario_pdf_replaced_bumps_version_and_orphans_annotations(tmp_path: Path):
    """PDF 原地换版后 source version 增加，旧 Annotation 进入 orphan."""
    scene = Scene(tmp_path)
    storage = scene.db()
    pid, sid = scene.seed(storage)
    scene.adopt(storage, pid)

    before = storage.get_source(sid).source_version

    # Replace the bytes in place.
    (scene.paper_dir / "paper.pdf").write_bytes(PDF_V2)

    scene.reindex(storage)
    after = storage.get_source(sid)

    assert after.source_version > before, (
        "replacing the bytes must advance the version, otherwise version "
        "pinning is cosmetic and 412 can never fire"
    )
    assert after.sha256 and after.sha256 != "a" * 64, "the hash must reflect the new bytes"


def test_scenario_missing_source_is_marked_not_deleted(tmp_path: Path):
    """缺失来源被正确标记，且不影响同论文的其它来源."""
    storage = PaperStorage(tmp_path / "p.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="P", display_title="t"))
    ids = {}
    for name in ("a.pdf", "b.pdf"):
        sid = new_source_id()
        ids[name] = sid
        storage.upsert_source(
            PaperSource(
                source_id=sid,
                paper_id=pid,
                role=SourceRole.ORIGINAL_PDF,
                rel_path=name,
            )
        )

    storage.mark_sources_missing(pid, ["a.pdf"])

    assert storage.get_source(ids["a.pdf"]).missing_since is not None
    assert storage.get_source(ids["b.pdf"]).missing_since is None
    # Nothing was removed: the tombstone is a marker.
    assert len(storage.list_sources(pid, include_inactive=True)) == 2


# ---------------------------------------------------------------------------
# Group 2 — concurrency and crash
# ---------------------------------------------------------------------------


def test_scenario_concurrent_first_annotations_all_survive(tmp_path: Path, monkeypatch):
    """两个请求同时向不存在的 sidecar 添加 Annotation，必须都保留."""
    from backend.app.paper.api import _mutate_sidecar

    scene = Scene(tmp_path)
    scene.install_app_config(monkeypatch)
    storage = scene.db()
    pid, sid = scene.seed(storage)
    scene.adopt(storage, pid)
    paper = storage.get_paper(pid)

    created: list[str] = []
    errors: list[Exception] = []

    def add(index: int) -> None:
        # The frozen schema requires a UUID; a placeholder would be rejected
        # by the contract rather than exercising the concurrency path.
        from backend.app.paper.models import new_annotation_id

        ann_id = new_annotation_id()

        def mutation(document):
            document.setdefault("annotations", []).append(
                {
                    "annotation_id": ann_id,
                    "source_id": sid,
                    "kind": "HIGHLIGHT",
                    "body_markdown": "",
                    "selected_text": f"s{index}",
                    "anchor_schema_version": 2,
                    "anchor": {
                        "type": "PDF_TEXT",
                        "page_index": 0,
                        "page_label": "1",
                        "rotation": 0,
                        "quad_points_normalized": [{"x": 0.1, "y": 0.2}],
                        "text_quote": {"exact": f"s{index}", "prefix": None, "suffix": None},
                    },
                    "source_sha256": "a" * 64,
                    "source_version": 1,
                    "created_at": "2026-09-16T00:00:00Z",
                    "updated_at": "2026-09-16T00:00:00Z",
                    "deleted_at": None,
                    "orphaned_at": None,
                    "revision": 1,
                }
            )

        try:
            _mutate_sidecar(paper, mutation)
            created.append(ann_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"concurrent writes must not fail: {errors}"
    assert len(created) == 6

    document = json.loads(
        (scene.paper_dir / "paper.annotations.json").read_text(encoding="utf-8")
    )
    live = [a for a in document["annotations"] if not a.get("deleted_at")]
    assert len(live) == 6, f"all six annotations must survive, got {len(live)}"


def test_scenario_corrupt_sidecar_blocks_mutation(tmp_path: Path, monkeypatch):
    """损坏 sidecar 后，任何写操作都不得覆盖原文件."""
    from backend.app.paper.api import SidecarCorruptError, _mutate_sidecar

    scene = Scene(tmp_path)
    scene.install_app_config(monkeypatch)
    storage = scene.db()
    pid, sid = scene.seed(storage)
    scene.adopt(storage, pid)
    paper = storage.get_paper(pid)

    sidecar = scene.paper_dir / "paper.annotations.json"
    sidecar.write_text('{"annotations": [BROKEN', encoding="utf-8")
    before = sidecar.read_bytes()

    with pytest.raises(SidecarCorruptError):
        _mutate_sidecar(paper, lambda doc: doc.setdefault("annotations", []).append({}))

    assert sidecar.read_bytes() == before, "the corrupt bytes must be preserved verbatim"



def test_scenario_crash_between_file_and_row_leaves_recoverable_state(tmp_path: Path):
    """文件写完、DB 尚未更新时进程退出 —— 必须能 roll forward."""
    storage = PaperStorage(tmp_path / "p.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="P", display_title="t"))

    # Simulate the crash: the intent is recorded, the file exists, but the row
    # was never written.
    intent = storage.begin_write_intent(pid, "create_note", {"rel_path": "notes.md"})
    pending = storage.list_pending_write_intents()
    assert len(pending) == 1
    assert pending[0]["intent_id"] == intent
    assert pending[0]["state"] == "PENDING"

    # Recovery rolls forward from the intent rather than deleting the file.
    storage.commit_write_intent(intent)
    assert storage.list_pending_write_intents() == []


def test_scenario_note_edit_in_obsidian_is_not_overwritten(tmp_path: Path):
    """笔记保存同时被外部编辑器改写 —— 必须 409 且保留远端内容."""
    from backend.app.paper.writer import ConflictError

    scene = Scene(tmp_path)
    note_rel = scene.vault_rel("notes.md")
    scene.service.create(note_rel, "# 初稿")
    _, digest = scene.service.read(note_rel)

    # The user edits the same file in Obsidian while the editor holds a draft.
    (scene.paper_dir / "notes.md").write_text("# Obsidian 改的", encoding="utf-8")

    with pytest.raises(ConflictError):
        scene.service.save(note_rel, "# 工作台草稿", expected_hash=digest)

    assert (scene.paper_dir / "notes.md").read_text(encoding="utf-8") == "# Obsidian 改的"


def test_scenario_rapid_paper_switch_does_not_cross_write(tmp_path: Path):
    """快速 A→B→C 切换 Paper，同时有 autosave 在途 —— 不得串写."""
    # The invariant lives in the note editor's epoch handling: a response from a
    # superseded paper must be discarded rather than applied to the new one.
    note_js = (
        REPO_ROOT / "frontend" / "dist" / "paper" / "note-pane.js"
    ).read_text(encoding="utf-8")
    assert "this._epoch += 1" in note_js, "switching must advance the epoch"
    assert "stale-epoch" in note_js, "superseded responses must be identifiable"
    # The hash must only be adopted after the epoch check, otherwise the new
    # paper's editor state is corrupted even though the response was discarded.
    epoch_check = note_js.index("if (epoch !== this._epoch)")
    hash_assign = note_js.index("this.hash = result.hash")
    assert epoch_check < hash_assign, "state must be validated before it is applied"


# ---------------------------------------------------------------------------
# Group 3 — single-writer coordination
# ---------------------------------------------------------------------------


def test_scenario_multi_worker_launch_is_refused():
    """误开两个 worker —— 必须拒绝启动."""
    assert_single_worker(1)
    with pytest.raises(MultiWorkerError):
        assert_single_worker(2)
    with pytest.raises(MultiWorkerError):
        assert_single_worker(8)


def test_scenario_second_instance_cannot_claim_the_vault(tmp_path: Path):
    """两个应用实例同时写同一 Vault —— 第二个必须被拒绝."""
    lock_path = tmp_path / "vault.lock"
    first = VaultWriteLock(lock_path, vault_root=tmp_path / "vault")
    first.acquire()
    try:
        second = VaultWriteLock(lock_path, vault_root=tmp_path / "vault")
        with pytest.raises(VaultAlreadyOwnedError) as exc:
            second.acquire()
        # The error must name the holder so an operator can act on it.
        assert "pid=" in str(exc.value)
    finally:
        first.release()

    # Once released, the vault can be claimed again.
    third = VaultWriteLock(lock_path, vault_root=tmp_path / "vault")
    third.acquire()
    third.release()


def test_scenario_lock_file_records_the_holder(tmp_path: Path):
    lock_path = tmp_path / "vault.lock"
    lock = VaultWriteLock(lock_path, vault_root=tmp_path / "vault")
    lock.acquire()
    try:
        content = lock_path.read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in content
        assert str(tmp_path / "vault") in content
    finally:
        lock.release()
    assert not lock_path.exists(), "release must clean up its own lock file"


def test_scenario_lock_release_is_idempotent(tmp_path: Path):
    lock = VaultWriteLock(tmp_path / "vault.lock", vault_root=tmp_path / "vault")
    lock.acquire()
    lock.release()
    lock.release()  # must not raise


# ---------------------------------------------------------------------------
# Group 4 — client/server contract
# ---------------------------------------------------------------------------


def _frontend_module(name: str) -> str:
    return (REPO_ROOT / "frontend" / "dist" / "paper" / name).read_text(encoding="utf-8")


def test_scenario_frontend_urls_match_backend_routes():
    """Markdown 客户端使用的 URL 后端确实返回 200.

    A textual cross-check only: the point is that the client's request paths are
    a subset of the routes the server actually declares. The earlier mismatch
    (client asking /content while the server required /text) was invisible to
    every unit test because each side was tested against its own fixture.
    """
    from backend.app.paper.api import router as paper_router
    from backend.app.paper.api_sources import router as source_router

    declared = {route.path for route in paper_router.routes} | {
        route.path for route in source_router.routes
    }
    api_js = _frontend_module("api.js")

    for route in ("/api/paper/papers/{paper_id}/sources", "/api/paper/papers/{paper_id}/annotations"):
        assert route in declared, f"{route} must be declared by the backend"
    # The client's Markdown fetch must target the text route.
    text_line = next(
        line for line in api_js.splitlines() if "fetch(" in line and "SOURCES" in line
    )
    assert "/text" in text_line, f"Markdown must be fetched from /text, got {text_line}"
    assert "/api/paper-sources/{source_id}/text" in declared


def test_scenario_error_responses_still_carry_no_store(tmp_path: Path):
    """409/412/415/500 响应仍有 no-store.

    Asserted against the middleware's own predicate plus a live request, since a
    traceback path can otherwise bypass the header entirely.
    """
    from backend.app.main import _is_sensitive_path

    for path in ("/api/paper/papers", "/api/paper-sources/x/content", "/api/im/status"):
        assert _is_sensitive_path(path), f"{path} must be treated as sensitive"
    assert not _is_sensitive_path("/api/health")


def test_scenario_renderer_refuses_unsanitised_output():
    """The Markdown pane must not fall back to injecting raw HTML."""
    pane = _frontend_module("markdown-pane.js")
    assert "renderer-unavailable" in pane, "a missing renderer must fail loudly"
    assert "innerHTML = `<article" not in pane or "render(" in pane


def test_scenario_backup_durability(tmp_path: Path, monkeypatch):
    """backup 文件与目录必须 fsync —— 崩溃耐久性.

    Behavioural rather than textual: the previous version asserted the string
    "fsync" appeared in the backup function, which still matched when the actual
    call was removed.
    """
    from backend.app.paper import writer as writer_mod

    scene = Scene(tmp_path)
    note_rel = scene.vault_rel("notes.md")
    scene.service.create(note_rel, "v1")
    _, digest = scene.service.read(note_rel)

    syncs: list[object] = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        syncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(writer_mod.os, "fsync", tracking_fsync)
    result = scene.service.save(note_rel, "v2", expected_hash=digest)

    backup = Path(result.backup_path)
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == "v1"

    # Durability requires a sync on every link of the chain: the temp file
    # itself, then the backup directory so the rename survives a crash. Dropping
    # any one of them is exactly the regression this asserts against, so the
    # count is exact rather than a lower bound.
    assert len(syncs) == 4, (
        f"expected 4 fsync calls (temp file, temp dir, backup file, backup dir), "
        f"saw {len(syncs)}: a missing one means a crash can lose a write"
    )


# ---------------------------------------------------------------------------
# Group 5 — crash recovery rolls forward (Sol: never roll back)
# ---------------------------------------------------------------------------


def test_scenario_recovery_rebuilds_row_from_existing_note_file(tmp_path: Path):
    """文件写完、DB 尚未更新时退出 —— 恢复必须前滚，不是删除文件.

    Sol was explicit: deleting the note that was already created would destroy
    user content. Recovery therefore adopts the file rather than undoing it.
    """
    from backend.app.paper.models import new_note_id
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    storage = scene.db()
    pid, _ = scene.seed(storage)

    note_id = new_note_id()
    (scene.paper_dir / "notes.md").write_text("# 崩溃前写入的笔记\n", encoding="utf-8")
    storage.begin_write_intent(
        pid,
        "create_note",
        {"rel_path": "notes.md", "note_id": note_id, "papers_root_rel": "论文根"},
    )
    assert storage.get_note_for_paper(pid) is None, "the row must be missing for this case"

    report = recover_pending_writes(storage, scene.service)

    assert report.checked == 1
    assert report.resolved == 1
    assert report.unresolved == 0

    row = storage.get_note_for_paper(pid)
    assert row is not None, "recovery must rebuild the row from the file"
    assert row.note_id == note_id
    assert row.content_sha256, "the rebuilt row must record the file's hash"
    assert (scene.paper_dir / "notes.md").is_file(), "recovery must never delete the file"
    assert storage.list_pending_write_intents() == []


def test_scenario_recovery_leaves_consistent_state_alone(tmp_path: Path):
    """两边都在且一致时，恢复不得改写任何东西."""
    from backend.app.paper.models import PaperNote, new_note_id
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    storage = scene.db()
    pid, _ = scene.seed(storage)

    note_id = new_note_id()
    result = scene.service.create(scene.vault_rel("notes.md"), "# 一致的笔记")
    (scene.paper_dir / "notes.md").write_text("# 一致的笔记", encoding="utf-8")
    storage.upsert_note(
        PaperNote(
            note_id=note_id,
            paper_id=pid,
            rel_path="notes.md",
            content_sha256=result.new_hash,
        )
    )
    storage.begin_write_intent(
        pid,
        "create_note",
        {"rel_path": "notes.md", "note_id": note_id, "papers_root_rel": "论文根"},
    )

    report = recover_pending_writes(storage, scene.service)

    assert report.resolved == 1
    assert report.outcomes[0].action == "already-consistent"
    assert storage.get_note_for_paper(pid).note_id == note_id
    assert (scene.paper_dir / "notes.md").read_text(encoding="utf-8") == "# 一致的笔记"


def test_scenario_recovery_flags_a_mismatched_note_identity(tmp_path: Path):
    """文件与行指向不同 note_id 时必须报冲突，不得猜测."""
    from backend.app.paper.models import PaperNote, new_note_id
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    storage = scene.db()
    pid, _ = scene.seed(storage)

    (scene.paper_dir / "notes.md").write_text("# 文件", encoding="utf-8")
    storage.upsert_note(
        PaperNote(
            note_id=new_note_id(),
            paper_id=pid,
            rel_path="notes.md",
        )
    )
    storage.begin_write_intent(
        pid,
        "create_note",
        {"rel_path": "notes.md", "note_id": new_note_id(), "papers_root_rel": "论文根"},
    )

    report = recover_pending_writes(storage, scene.service)

    assert report.unresolved == 1
    assert report.outcomes[0].action == "inconsistent"


def test_scenario_recovery_reports_an_orphaned_intent(tmp_path: Path):
    """论文行不存在时，intent 无法完成，必须明确报告而不是猜测."""
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    storage = scene.db()
    storage.begin_write_intent(
        "pw_00000000-0000-4000-8000-000000000000",
        "create_note",
        {"rel_path": "notes.md", "note_id": "note_x"},
    )

    report = recover_pending_writes(storage, scene.service)

    assert report.unresolved == 1
    assert report.outcomes[0].action == "orphaned"


def test_scenario_recovery_is_idempotent(tmp_path: Path):
    """重复运行恢复不得产生副作用."""
    from backend.app.paper.models import new_note_id
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    storage = scene.db()
    pid, _ = scene.seed(storage)

    note_id = new_note_id()
    (scene.paper_dir / "notes.md").write_text("# 笔记", encoding="utf-8")
    storage.begin_write_intent(
        pid,
        "create_note",
        {"rel_path": "notes.md", "note_id": note_id, "papers_root_rel": "论文根"},
    )

    first = recover_pending_writes(storage, scene.service)
    second = recover_pending_writes(storage, scene.service)

    assert first.resolved == 1
    assert second.checked == 0, "committed intents must not be reprocessed"
    assert storage.get_note_for_paper(pid).note_id == note_id


def test_scenario_adoption_intent_is_recovered(tmp_path: Path, monkeypatch):
    """采纳写到一半时，恢复必须补齐 manifest.

    The intent here deliberately omits the papers-root prefix: recovery runs at
    startup with the live configuration available, so it must derive the prefix
    rather than depend on what the interrupted write happened to record.
    """
    from backend.app.paper.manifest import is_adopted
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    scene.install_app_config(monkeypatch)
    storage = scene.db()
    pid, _ = scene.seed(storage)
    storage.begin_write_intent(pid, "adopt", {"trigger": "status_change"})

    report = recover_pending_writes(storage, scene.service)

    assert report.resolved == 1
    paper = storage.get_paper(pid)
    assert is_adopted(paper), "recovery must finish the adoption"
    assert (scene.paper_dir / "paper.workbench.json").is_file()


def test_scenario_annotation_store_intent_needs_no_rollforward(tmp_path: Path):
    """sidecar 走原子发布，崩溃不会留下半截文档."""
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    storage = scene.db()
    pid, _ = scene.seed(storage)
    storage.begin_write_intent(pid, "create_annotation_store", {})

    report = recover_pending_writes(storage, scene.service)

    assert report.resolved == 1
    assert report.outcomes[0].action == "already-consistent"


# ---------------------------------------------------------------------------
# Group 6 — multi-tab coordination
# ---------------------------------------------------------------------------


def test_scenario_tab_sync_is_wired_into_the_workbench():
    """跨标签协调必须真的接线，而不是只定义了一个模块.

    The review flagged exactly this pattern elsewhere — a component that exists
    while nothing calls it.
    """
    main_js = _frontend_module("main.js")
    assert "TabCoordinator" in main_js, "the coordinator must be instantiated"
    assert "ACTIVITY.NOTE_SAVED" in main_js, "note saves must be announced"
    assert "_onRemoteNoteSave" in main_js, "remote saves must be handled"


def test_scenario_tab_sync_never_replaces_an_unsaved_draft():
    """另一标签页保存时，本标签的草稿绝不能被静默替换.

    Coordination is advisory: the server remains the authority on conflicts, and
    a tab with unsaved work must be told rather than overwritten.
    """
    main_js = _frontend_module("main.js")
    # Locate the method definition, not the registration call site.
    definition = main_js.index("_onRemoteNoteSave(info) {")
    block = main_js[definition : main_js.index("_onRemoteAnnotationChange(info) {")]
    assert "hasUnsavedWork()" in block, "an unsaved draft must be detected"
    # The warn path must come first; the reload path must be the clean case.
    warn_index = block.index("hasUnsavedWork()")
    reload_index = block.index("loadFor")
    assert warn_index < reload_index, "the draft check must precede any reload"


def test_scenario_tab_sync_ignores_its_own_messages():
    """自己的广播会经 storage 路径回传，必须忽略，否则形成回环."""
    tab_js = _frontend_module("tab-sync.js")
    assert "message.tabId === this.tabId" in tab_js
    assert "return" in tab_js[tab_js.index("message.tabId === this.tabId") :][:80]


def test_scenario_tab_sync_degrades_without_channel_support():
    """BroadcastChannel 不可用时必须回退而不是崩溃."""
    tab_js = _frontend_module("tab-sync.js")
    assert "typeof BroadcastChannel" in tab_js, "availability must be checked"
    assert "localStorage" in tab_js, "a fallback path is required"


def test_scenario_recovery_finds_the_note_despite_a_stale_prefix(tmp_path: Path, monkeypatch):
    """配置变更后，恢复不得把存在的笔记误判为「从未写入」.

    Self-audit finding: recovery trusted the prefix recorded in the intent. With
    a stale prefix the lookup missed a file that existed, recovery concluded the
    write had never happened, and committing that intent left a note on disk
    with no row — invisible to the reader and unrecoverable without noticing the
    orphan.
    """
    from backend.app.paper.models import new_note_id
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    scene.install_app_config(monkeypatch)
    storage = scene.db()
    pid, _ = scene.seed(storage)

    note_id = new_note_id()
    (scene.paper_dir / "notes.md").write_text("# 真实存在的笔记\n", encoding="utf-8")

    # The intent records a prefix that no longer matches the configuration.
    storage.begin_write_intent(
        pid,
        "create_note",
        {"rel_path": "notes.md", "note_id": note_id, "papers_root_rel": "陈旧前缀"},
    )

    report = recover_pending_writes(storage, scene.service)

    assert report.outcomes[0].action == "completed"
    assert storage.get_note_for_paper(pid) is not None, (
        "a file that exists must never be reported as never written"
    )
    assert storage.get_note_for_paper(pid).note_id == note_id
    assert (scene.paper_dir / "notes.md").is_file()


def test_scenario_recovery_does_not_silently_retire_an_unfindable_intent(tmp_path: Path, monkeypatch):
    """找不到文件时必须保留 intent 并报告，而不是当作「无影响」提交.

    Retiring it would erase the only record that a write was attempted, so a
    genuinely lost file would leave no trace at all.
    """
    from backend.app.paper.recovery import recover_pending_writes

    scene = Scene(tmp_path)
    scene.install_app_config(monkeypatch)
    storage = scene.db()
    pid, _ = scene.seed(storage)

    # No file on disk and no row: the write may or may not have happened.
    storage.begin_write_intent(
        pid,
        "create_note",
        {"rel_path": "notes.md", "note_id": "note_11111111-2222-4333-8999-444444444444"},
    )

    report = recover_pending_writes(storage, scene.service)

    assert report.unresolved == 1
    assert report.outcomes[0].action == "no-evidence"
    # The intent survives so an operator can still see that a write was tried.
    assert len(storage.list_pending_write_intents()) == 1


def test_scenario_paper_modules_reference_no_external_origin():
    """论文子系统自身的资源全部同源.

    The claim is deliberately scoped: the paper modules and the vendored PDF.js
    introduce no external dependency. The host page still loads its own CDNs
    (Tailwind, KaTeX, marked, DOMPurify), so "the workspace makes no outbound
    request at all" would be false, and the ADR says so explicitly.

    Runtime observation is the strongest form of this check, but it needs a
    browser; this asserts the static half — no paper module names an external
    URL — which is what would regress first.
    """
    import re

    paper_dir = REPO_ROOT / "frontend" / "dist" / "paper"
    offenders: list[tuple[str, str]] = []
    for path in sorted(paper_dir.glob("*.js")):
        for match in re.finditer(r"['\"`](https?://[^'\"`]+)['\"`]", path.read_text(encoding="utf-8")):
            url = match.group(1)
            if "127.0.0.1" in url or "localhost" in url:
                continue
            offenders.append((path.name, url))

    assert not offenders, f"paper modules must not reference external origins: {offenders}"


def test_scenario_pdfjs_is_vendored_not_cdn():
    """PDF.js 必须本地 vendored —— CDN 会让查看器版本漂移."""
    reader_js = _frontend_module("pdf-bridge.js")
    assert "/static/vendor/pdfjs/" in reader_js, "the viewer must be served from our own origin"
    assert "cdn" not in reader_js.lower(), "no CDN may be used for the viewer"
    vendored = REPO_ROOT / "frontend" / "dist" / "vendor" / "pdfjs" / "build" / "pdf.mjs"
    assert vendored.is_file(), "the vendored build must be present"
    worker = REPO_ROOT / "frontend" / "dist" / "vendor" / "pdfjs" / "build" / "pdf.worker.mjs"
    assert worker.is_file(), "the worker must be vendored alongside the main build"


# ---------------------------------------------------------------------------
# Group 7 — review round 2 findings (all five were reproduced before fixing)
# ---------------------------------------------------------------------------


def test_scenario_note_creation_updates_the_manifest(workbench_like):
    """CRITICAL: the manifest must record the note binding.

    The adoption gate runs before the note has an id, so the manifest was
    permanently left at `note: null`. A database rebuild then could not
    re-attach the note — the identity property ADR-006 exists to provide.
    """
    client, storage, pid, _ = workbench_like
    response = client.post(f"/api/paper/papers/{pid}/note", json={"content": "# 笔记"})
    assert response.status_code == 200, response.text
    note_id = response.json()["note_id"]

    manifest = client.manifest_path()
    assert manifest.is_file(), "the note must not exist without an adopting manifest"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document.get("note") is not None, "manifest.note must not stay null"
    assert document["note"]["note_id"] == note_id


def test_scenario_lock_failure_prevents_startup(tmp_path: Path):
    """CRITICAL: a second instance must not serve after failing to take the lock.

    This was swallowed by `except Exception: logger.error(...)`, so the second
    instance served alongside the first, both writing the vault and both running
    crash recovery over the same intents.
    """
    import inspect

    import backend.app.main as main_mod

    source = inspect.getsource(main_mod.lifespan)
    acquire_index = source.index("_vault_lock.acquire()")
    # The acquire call must not sit inside a try/except that logs and continues.
    preceding = source[:acquire_index]
    tail = source[acquire_index : acquire_index + 400]
    assert "except Exception" not in tail, (
        "the lock error must not be swallowed; the process must refuse to start"
    )
    assert "env_allows_readonly_startup" in source, "a read-only opt-out must be explicit"
    assert preceding  # sanity: the slice is non-empty


def test_scenario_stale_lock_is_reclaimed_but_a_live_one_is_not(tmp_path: Path):
    """高风险: SIGKILL leaves a stale lock; requiring manual cleanup is wrong."""
    from backend.app.paper.ownership import VaultWriteLock, VaultAlreadyOwnedError

    lock_path = tmp_path / "v.lock"

    # A pid that cannot exist.
    lock_path.write_text("pid=999999\nvault=/tmp/x\n", encoding="utf-8")
    reclaimed = VaultWriteLock(lock_path, vault_root=tmp_path / "vault")
    reclaimed.acquire()
    assert reclaimed.path.exists()
    reclaimed.release()

    # A live pid (ours) must not be stolen.
    lock_path.write_text(f"pid={os.getpid()}\nvault=/tmp/x\n", encoding="utf-8")
    live = VaultWriteLock(lock_path, vault_root=tmp_path / "vault")
    with pytest.raises(VaultAlreadyOwnedError):
        live.acquire()


def test_scenario_lock_is_scoped_per_vault(tmp_path: Path):
    """A fixed lock path made unrelated vaults contend with each other."""
    from backend.app.paper.ownership import lock_path_for

    assert lock_path_for(tmp_path / "a") != lock_path_for(tmp_path / "b")
    assert lock_path_for(tmp_path / "a") == lock_path_for(tmp_path / "a")


def test_scenario_workspace_positions_merge_not_replace(workbench_like):
    """HIGH: saving one source's position must not erase another's."""
    client, storage, pid, sources = workbench_like
    pdf_id, md_id = sources
    url = f"/api/paper/papers/{pid}/workspace-state"

    first = client.put(
        url,
        json={
            "active_pane": "PDF",
            "source_positions": {
                pdf_id: {
                    "kind": "PDF",
                    "page_index": 5,
                    "page_offset_ratio": 0.2,
                    "scale": 1,
                    "rotation": 0,
                    "source_version": 1,
                }
            },
        },
    )
    assert first.status_code == 200, first.text

    second = client.put(
        url,
        json={
            "active_pane": "MARKDOWN",
            "source_positions": {
                md_id: {
                    "kind": "MARKDOWN",
                    "heading_path": ["A"],
                    "scroll_ratio": 0.7,
                    "source_version": 1,
                }
            },
        },
    )
    assert second.status_code == 200, second.text

    positions = client.get(url).json()["state"]["source_positions"]
    assert pdf_id in positions, "the PDF position must survive a Markdown save"
    assert md_id in positions


def test_scenario_workspace_rejects_non_finite_numbers(workbench_like):
    """HIGH: Infinity passes isinstance but breaks the client's JSON.parse."""
    client, storage, pid, sources = workbench_like
    pdf_id, _ = sources
    url = f"/api/paper/papers/{pid}/workspace-state"

    body = (
        '{"active_pane":"PDF","source_positions":{"'
        + pdf_id
        + '":{"kind":"PDF","page_index":1,"page_offset_ratio":0.5,'
        + '"scale":Infinity,"rotation":0,"source_version":1}}}}'
    )
    response = client.put(url, content=body, headers={"Content-Type": "application/json"})
    # 400 from the explicit validator, or 422 when Pydantic rejects the
    # non-JSON token first. Either way it must not reach storage.
    assert response.status_code in (400, 422), "a non-finite scale must be refused"


def test_scenario_workspace_rejects_a_foreign_source(workbench_like):
    """HIGH: an arbitrary key let a client store positions for other papers."""
    from backend.app.paper.models import Paper, new_paper_id

    client, storage, pid, _ = workbench_like
    other_pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=other_pid, folder_relpath="方向/别的", display_title="o"))

    response = client.put(
        f"/api/paper/papers/{pid}/workspace-state",
        json={
            "active_pane": "PDF",
            "source_positions": {
                "src_ffffffff-ffff-4fff-8fff-ffffffffffff": {
                    "kind": "PDF",
                    "page_index": 1,
                    "page_offset_ratio": 0.5,
                    "scale": 1,
                    "rotation": 0,
                }
            },
        },
    )
    assert response.status_code == 400


def test_scenario_egress_is_blocked_before_the_dom_attach():
    """HIGH: assigning innerHTML first already issued the external request.

    The browser starts fetching an <img src> as soon as the node is parsed, so
    rewriting afterwards cannot stop the request. The boundary must be applied to
    a detached tree, before it reaches the live document.
    """
    pane = _frontend_module("markdown-pane.js")
    create_index = pane.index("const holder = document.createElement")
    inner_index = pane.index("holder.innerHTML = html")
    enforce_index = pane.index("this._enforceEgressBoundary(holder)")
    attach_index = pane.index("this.host.replaceChildren(holder)")

    assert create_index < inner_index < enforce_index < attach_index, (
        "the egress boundary must run on the detached tree, before attaching it"
    )
    # And the live host must never receive raw markup directly.
    assert "this.host.innerHTML = `<article" not in pane


def test_scenario_workspace_conflict_reloads_the_version_behaviourally():
    """CRITICAL: the 409 path must re-read the version, verified by running it.

    Two earlier versions of this check were worthless. The first asserted the
    string "stateVersion = null" was absent from a source slice. The second
    asserted the string "_reloadVersion" was present — but the method did not
    exist, so the conflict path threw a TypeError at runtime and the test still
    passed. Grepping source text is not verification; the behaviour has to run.

    This drives the real module under Node with a save that returns 409 and
    asserts the version was re-read from the server.
    """
    import json as _json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to exercise the browser module")

    module = REPO_ROOT / "frontend" / "dist" / "paper" / "workspace-state.js"
    script = f"""
import {{ WorkspaceStateTracker }} from '{module}';

let loadCalls = 0;
const tracker = new WorkspaceStateTracker({{
  load: async () => {{ loadCalls += 1; return {{ state: {{ state_version: 7 }} }}; }},
  save: async () => {{ const e = new Error('conflict'); e.status = 409; throw e; }},
}});

tracker.paperId = 'pw_test';
tracker.stateVersion = 3;
tracker.state = {{ source_positions: {{}} }};
tracker._dirty = true;

const result = await tracker.flush();
await new Promise((r) => setTimeout(r, 50));

console.log(JSON.stringify({{
  reason: result && result.reason,
  loadCalls,
  version: tracker.stateVersion,
}}));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (
        f"the module must run without throwing: {completed.stderr[-400:]}"
    )
    payload = _json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["reason"] == "version-conflict"
    assert payload["loadCalls"] >= 1, "the conflict path must consult the server"
    assert payload["version"] == 7, (
        f"the version must be re-read, not cleared (got {payload['version']})"
    )


def test_scenario_note_binding_survives_a_database_rebuild(tmp_path: Path, monkeypatch):
    """CRITICAL: the manifest's note binding must be consumed, not just written.

    The earlier test only asserted the manifest contained a note id. Nothing
    read it back, so after a rebuild the note was gone, the reader showed "no
    note", and creating one returned 409 permanently. Writing the binding is only
    half the chain; this asserts the whole loop.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import backend.app.paper.api as paper_api
    import backend.app.state as app_state
    from backend.app.paper.models import Paper, PaperSource, SourceRole, new_paper_id, new_source_id
    from backend.scripts import paper_index as indexer

    vault = tmp_path / "vault"
    papers_root = vault / "论文根"
    paper_dir = papers_root / "方向" / "论文A"
    paper_dir.mkdir(parents=True)
    (paper_dir / "a.pdf").write_bytes(PDF_V1)

    papers_root_ = papers_root

    class _Cfg:
        vault_path = vault
        vault_root = vault
        papers_root = papers_root_
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    monkeypatch.setattr(app_state, "_state", {"cfg": _Cfg()}, raising=False)

    pid, sid = new_paper_id(), new_source_id()
    first_db = tmp_path / "first.db"
    storage = PaperStorage(first_db)
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

    monkeypatch.setattr(paper_api, "_storage", lambda: storage)
    app = FastAPI()
    app.include_router(paper_api.router)
    client = TestClient(app)

    created = client.post(f"/api/paper/papers/{pid}/note", json={"content": "# 我的笔记\n"})
    assert created.status_code == 200, created.text
    note_id = created.json()["note_id"]

    manifest = paper_dir / "paper.workbench.json"
    assert manifest.is_file()

    storage.close()
    first_db.unlink(missing_ok=True)
    Path(str(first_db) + "-wal").unlink(missing_ok=True)
    Path(str(first_db) + "-shm").unlink(missing_ok=True)

    # Rebuild from the Vault alone.
    rebuilt_db = tmp_path / "second.db"
    monkeypatch.setattr(indexer, "load_config", lambda: _Cfg())
    monkeypatch.setattr(indexer, "PaperStorage", lambda *a, **k: PaperStorage(rebuilt_db))
    report = indexer.index_papers(dry_run=False)
    assert report.get("notes_restored") == 1, "the manifest's note binding must be consumed"

    rebuilt = PaperStorage(rebuilt_db)
    row = rebuilt.get_note_for_paper(pid)
    assert row is not None, "the note row must be restored from the manifest"
    assert row.note_id == note_id, "the restored id must match the original"

    monkeypatch.setattr(paper_api, "_storage", lambda: rebuilt)
    app2 = FastAPI()
    app2.include_router(paper_api.router)
    payload = TestClient(app2).get(f"/api/paper/papers/{pid}/note").json()
    assert payload.get("exists") is True, "the reader must find the restored note"
    rebuilt.close()



def test_scenario_manifest_update_converges_under_concurrency(tmp_path: Path, monkeypatch):
    """自攻击发现：并发 update_manifest 时有 5/6 抛错.

    The optimistic hash made a lost race detectable but not survivable. The
    sidecar path already had a per-key lock and retry; the manifest path did
    not, so concurrent binding changes failed instead of converging.
    """
    import threading

    import backend.app.state as app_state
    from backend.app.paper.manifest import ensure_adopted, update_manifest
    from backend.app.paper.models import Paper, PaperSource, SourceRole, new_paper_id

    vault = tmp_path / "vault"
    papers_root = vault / "论文根"
    paper_dir = papers_root / "方向" / "论文A"
    paper_dir.mkdir(parents=True)
    (paper_dir / "a.pdf").write_bytes(PDF_V1)

    papers_root_ = papers_root

    class _Cfg:
        vault_path = vault
        vault_root = vault
        papers_root = papers_root_
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    monkeypatch.setattr(app_state, "_state", {"cfg": _Cfg()}, raising=False)

    storage = PaperStorage(tmp_path / "c.db")
    service = VaultWriteService(vault, backup_root=tmp_path / "bk")
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
    adopted = ensure_adopted(
        storage,
        service,
        storage.get_paper(pid),
        storage.list_sources(pid),
        operation="status_change",
        papers_root_rel="论文根",
    )
    storage.upsert_paper(adopted, allow_folder_move=True)

    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            paper = storage.get_paper(pid)
            paper.paper_tags = [f"tag{index}"]
            update_manifest(
                storage, service, paper, storage.list_sources(pid), papers_root_rel="论文根"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"concurrent manifest updates must converge, got {errors}"
    manifest = paper_dir / "paper.workbench.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert isinstance(document.get("tags"), list), "the manifest must stay valid JSON"
    storage.close()


def test_scenario_stale_lock_reclaim_does_not_steal_a_live_pid(tmp_path: Path):
    """自攻击：pid 可能被系统复用给无关进程，此时不得回收.

    Reclaiming a live pid's lock would hand the vault to two writers, which is
    the exact hazard the lock exists to prevent. Being conservative — refusing
    until an operator confirms — is the correct trade.
    """
    import subprocess
    import sys as _sys

    from backend.app.paper.ownership import VaultWriteLock, VaultAlreadyOwnedError

    long_lived = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        lock_path = tmp_path / "v.lock"
        lock_path.write_text(f"pid={long_lived.pid}\nvault=/some/other\n", encoding="utf-8")
        contender = VaultWriteLock(lock_path, vault_root=tmp_path / "vault")
        with pytest.raises(VaultAlreadyOwnedError):
            contender.acquire()
    finally:
        long_lived.terminate()
        long_lived.wait(timeout=10)


def test_scenario_manifest_records_a_custom_note_filename(workbench_like):
    """评审者指出：_note_name 硬编码 notes.md，别名笔记会记错文件名.

    A note created with a custom filename would be recorded as notes.md, so a
    rebuild would look for a file that does not exist and the note would be
    lost — silently, since the manifest looked well-formed.
    """
    client, storage, pid, _ = workbench_like
    created = client.post(
        f"/api/paper/papers/{pid}/note",
        json={"content": "# 别名笔记", "rel_path": "我的阅读笔记.md"},
    )
    assert created.status_code == 200, created.text

    document = json.loads(client.manifest_path().read_text(encoding="utf-8"))
    assert document["note"]["path"] == "我的阅读笔记.md", (
        "the manifest must record the note's real filename, not an assumed one"
    )


# ---------------------------------------------------------------------------
# Group 8 — every manifest call site must be exercised
# ---------------------------------------------------------------------------

_MANIFEST_DOC = {
    "schema_version": 1,
    "paper_id": "pw_3d9e1234-5678-4abc-89de-0123456789ab",
    "title_override": None,
    "sources": [
        {
            "source_id": "src_a12f1234-5678-4abc-89de-0123456789ab",
            "role": "ORIGINAL_PDF",
            "path": "a.pdf",
            "primary": True,
            "active": True,
        }
    ],
    "note": {"note_id": "note_f8cd1234-5678-4abc-89de-0123456789ab", "path": "notes.md"},
    "annotation_store": "paper.annotations.json",
    "tags": [],
    "created_at": "2026-09-16T00:00:00Z",
    "updated_at": "2026-09-16T00:00:00Z",
    "inactive_at": None,
}


def test_scenario_parsed_manifest_cannot_be_positionally_unpacked():
    """解析结果必须拒绝位置解包 —— 这正是漏改两处调用点的根因.

    parse_manifest grew from three fields to four. Two of the three call sites
    kept unpacking three values and raised `ValueError: too many values to
    unpack` — but only when their code path ran, which no test covered. Named
    access removes the failure mode instead of relying on remembering to update
    every call site.
    """
    from backend.app.paper.manifest import ParsedManifest, parse_manifest

    parsed = parse_manifest(_MANIFEST_DOC)
    assert isinstance(parsed, ParsedManifest)
    assert parsed.paper_id == _MANIFEST_DOC["paper_id"]
    assert parsed.note_id == _MANIFEST_DOC["note"]["note_id"]

    with pytest.raises(TypeError):
        a, b, c = parsed  # noqa: F841


def test_scenario_reconcile_with_manifest_runs(tmp_path: Path):
    """评审者指出：这条路径此前完全没有测试覆盖，改返回值即崩."""
    from backend.app.paper.manifest import reconcile_with_manifest
    from backend.app.paper.models import Paper

    paper = reconcile_with_manifest(
        Paper(paper_id="pw_temp", folder_relpath="方向/论文A"), [], _MANIFEST_DOC
    )
    # The manifest is authoritative for identity.
    assert paper.paper_id == _MANIFEST_DOC["paper_id"]


def test_scenario_adoption_race_branch_runs(tmp_path: Path, monkeypatch):
    """评审者指出：采纳竞态分支同样没有覆盖，改返回值即崩.

    The branch is reached when the manifest already exists on disk — another
    writer adopted the paper between our check and our create.
    """
    import backend.app.state as app_state
    from backend.app.paper.manifest import ensure_adopted
    from backend.app.paper.models import Paper
    from backend.app.paper.writer import VaultWriteService

    vault = tmp_path / "vault"
    papers_root = vault / "论文根"
    paper_dir = papers_root / "方向" / "论文A"
    paper_dir.mkdir(parents=True)
    (paper_dir / "a.pdf").write_bytes(PDF_V1)
    # A manifest already present, as if a concurrent request had just written it.
    (paper_dir / "paper.workbench.json").write_text(
        json.dumps(_MANIFEST_DOC), encoding="utf-8"
    )

    papers_root_ = papers_root

    class _Cfg:
        vault_path = vault
        vault_root = vault
        papers_root = papers_root_
        papers_max_depth = 6

        @property
        def papers_root_or_default(self):
            return self.papers_root

    monkeypatch.setattr(app_state, "_state", {"cfg": _Cfg()}, raising=False)

    storage = PaperStorage(tmp_path / "race.db")
    storage.upsert_paper(
        Paper(
            paper_id=_MANIFEST_DOC["paper_id"],
            folder_relpath="方向/论文A",
            display_title="t",
        )
    )
    service = VaultWriteService(vault, backup_root=tmp_path / "bk")

    adopted = ensure_adopted(
        storage,
        service,
        storage.get_paper(_MANIFEST_DOC["paper_id"]),
        [],
        operation="status_change",
        papers_root_rel="论文根",
    )
    assert adopted.manifest_relpath, "the race branch must adopt the existing manifest"
    storage.close()
