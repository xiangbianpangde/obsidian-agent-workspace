"""Scanner + storage tests for the Paper Workbench.

The scanner's contract is **precision over recall**: a wrong automatic binding
silently attaches the wrong file to a paper, which is worse than leaving the
paper AMBIGUOUS for a human. These tests therefore assert negative outcomes
(``test_..._is_not_bound``) at least as heavily as positive ones.

The vault shapes exercised here were taken from the real vault
(``02. 🟡 归类 Arrange/论文``), which contains 253+ paper folders, 620 PDFs,
414 translation files and 85+ MinerU artifact containers.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from backend.app.paper.models import (
    BindingState,
    MediaKind,
    Paper,
    PaperNote,
    PaperSource,
    PaperStatus,
    SourceRole,
    WorkspaceState,
    new_note_id,
    new_paper_id,
    new_source_id,
)
from backend.app.paper.scanner import (
    ScanConfig,
    classify_markdown_role,
    discover_papers,
    is_mineru_artifact_dir,
    normalize_title,
)
from backend.app.paper.storage import SCHEMA_VERSION, PaperStorage

PDF_BYTES = b"%PDF-1.4\n%stub\n"


def _write_pdf(path: Path) -> None:
    path.write_bytes(PDF_BYTES)


def _write_md(path: Path, text: str = "# x\n") -> None:
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures: the seven vault shapes the scanner must handle
# ---------------------------------------------------------------------------

@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault reproducing every shape observed in the real vault."""
    root = tmp_path / "论文"
    root.mkdir()

    # 1. Standard bilingual pair
    p1 = root / "01-智能体" / "面向 LLM 智能体的不确定性量化"
    p1.mkdir(parents=True)
    _write_pdf(p1 / "面向 LLM 智能体的不确定性量化.pdf")
    _write_md(p1 / "面向 LLM 智能体的不确定性量化_翻译导读.md")

    # 2. Guide + full translation (two translation variants)
    p2 = root / "01-智能体" / "Agent橙皮书"
    p2.mkdir(parents=True)
    _write_pdf(p2 / "Agent橙皮书.pdf")
    _write_md(p2 / "Agent橙皮书_翻译导读.md")
    _write_md(p2 / "Agent橙皮书_全文翻译.md")

    # 3. MinerU nesting: Paper/Paper/auto/{full.md,...}  (real vault shape)
    p3 = root / "04-Harness执行框架" / "SkillZipPro"
    auto = p3 / "SkillZipPro" / "auto"
    auto.mkdir(parents=True)
    _write_pdf(p3 / "SkillZipPro.pdf")
    _write_md(p3 / "SkillZipPro_翻译导读.md")
    _write_md(auto / "SkillZipPro.md")
    _write_pdf(auto / "SkillZipPro_origin.pdf")
    _write_pdf(auto / "SkillZipPro_layout.pdf")
    _write_pdf(auto / "SkillZipPro_span.pdf")
    (auto / "SkillZipPro_model.json").write_text("{}", encoding="utf-8")
    (auto / "SkillZipPro_middle.json").write_text("{}", encoding="utf-8")
    (auto / "SkillZipPro_content_list.json").write_text("[]", encoding="utf-8")
    (auto / "images").mkdir()

    # 4. Multiple PDFs, one matching the folder name
    p4 = root / "02-上下文工程" / "LongContext"
    p4.mkdir(parents=True)
    _write_pdf(p4 / "LongContext.pdf")
    _write_pdf(p4 / "LongContext_appendix.pdf")
    _write_md(p4 / "LongContext_翻译导读.md")

    # 5. Fully custom markdown name (no recognised suffix)
    p5 = root / "03-提示词工程" / "元上下文学习"
    p5.mkdir(parents=True)
    _write_pdf(p5 / "元上下文学习.pdf")
    _write_md(p5 / "元上下文学习.md")

    # 6. PDF-only paper (legal per ADR-006)
    p6 = root / "05-循环工程" / "SELF-REFINE"
    p6.mkdir(parents=True)
    _write_pdf(p6 / "SELF-REFINE.pdf")

    # 7. Category index file must never become a paper source
    idx = root / "其他方向"
    idx.mkdir()
    _write_md(idx / "00-索引.md")

    return root


# ---------------------------------------------------------------------------
# Filename classification
# ---------------------------------------------------------------------------

def test_classify_translation_full():
    assert classify_markdown_role("x_全文翻译.md") is SourceRole.TRANSLATION_FULL


def test_classify_translation_guide():
    assert classify_markdown_role("x_翻译导读.md") is SourceRole.TRANSLATION_GUIDE


def test_classify_chinese_translation_as_full():
    assert classify_markdown_role("x_中文翻译.md") is SourceRole.TRANSLATION_FULL


def test_classify_mineru_full_md_is_extraction_not_translation():
    """full.md is a MinerU extraction and must not masquerade as 中文翻译."""
    role = classify_markdown_role("full.md")
    assert role is SourceRole.EXTRACTED_MARKDOWN
    assert not role.is_translation


def test_classify_unknown_markdown_is_other():
    assert classify_markdown_role("元上下文学习.md") is SourceRole.OTHER_MARKDOWN


def test_classify_category_index_is_not_a_source():
    assert classify_markdown_role("00-索引.md") is None


def test_classify_readme_is_not_a_source():
    assert classify_markdown_role("README.md") is None


# ---------------------------------------------------------------------------
# MinerU detection
# ---------------------------------------------------------------------------

def test_mineru_dir_detected_via_multiple_markers(tmp_path: Path):
    d = tmp_path / "auto"
    d.mkdir()
    (d / "x_model.json").write_text("{}", encoding="utf-8")
    (d / "x_layout.pdf").write_bytes(PDF_BYTES)
    assert is_mineru_artifact_dir(d)


def test_plain_dir_with_single_full_md_is_not_mineru(tmp_path: Path):
    """One marker is not enough; a normal folder may contain full.md."""
    d = tmp_path / "notes"
    d.mkdir()
    (d / "full.md").write_text("# note", encoding="utf-8")
    assert not is_mineru_artifact_dir(d)


def test_normal_paper_folder_is_not_mineru(tmp_path: Path):
    d = tmp_path / "Attention"
    d.mkdir()
    _write_pdf(d / "Attention.pdf")
    _write_md(d / "Attention_翻译导读.md")
    assert not is_mineru_artifact_dir(d)


# ---------------------------------------------------------------------------
# Title normalisation
# ---------------------------------------------------------------------------

def test_normalize_strips_translation_suffix():
    assert normalize_title("Agent橙皮书_翻译导读") == normalize_title("Agent橙皮书")


def test_normalize_handles_fullwidth_punctuation():
    assert normalize_title("A：B") == normalize_title("A:B")


def test_normalize_is_nfc_stable():
    """macOS stores NFD; comparison must not depend on the on-disk form."""
    composed = "café"
    decomposed = "cafe\u0301"
    assert normalize_title(composed) == normalize_title(decomposed)


# ---------------------------------------------------------------------------
# Real-vault shape scan
# ---------------------------------------------------------------------------

def test_scan_finds_all_seven_papers(vault: Path):
    result = discover_papers(ScanConfig(root=vault))
    titles = {p.display_title for p in result.papers}
    assert "面向 LLM 智能体的不确定性量化" in titles
    assert "Agent橙皮书" in titles
    assert "SkillZipPro" in titles
    assert "LongContext" in titles
    assert "元上下文学习" in titles
    assert "SELF-REFINE" in titles
    assert len(result.papers) == 6


def test_scan_does_not_treat_mineru_auto_dir_as_a_paper(vault: Path):
    """Regression: the real vault nests Paper/Paper/auto/ and the naive
    scanner emitted a phantom paper literally named 'auto'."""
    result = discover_papers(ScanConfig(root=vault))
    titles = {p.display_title for p in result.papers}
    assert "auto" not in titles
    assert "images" not in titles


def test_scan_records_mineru_containers_separately(vault: Path):
    result = discover_papers(ScanConfig(root=vault))
    assert any("auto" in c for c in result.mineru_containers)


def test_scan_binds_two_translation_variants_separately(vault: Path):
    """ADR-006: guide and full translation are distinct sources, not one field."""
    result = discover_papers(ScanConfig(root=vault))
    paper = next(p for p in result.papers if p.display_title == "Agent橙皮书")
    roles = {s.role for s in paper.sources}
    assert SourceRole.TRANSLATION_GUIDE in roles
    assert SourceRole.TRANSLATION_FULL in roles
    assert paper.primary_translation_source_id is not None
    primary = next(
        s for s in paper.sources if s.source_id == paper.primary_translation_source_id
    )
    assert primary.role is SourceRole.TRANSLATION_FULL, "full translation wins display"


def test_scan_binds_all_pdfs_with_exactly_one_primary(vault: Path):
    result = discover_papers(ScanConfig(root=vault))
    paper = next(p for p in result.papers if p.display_title == "LongContext")
    pdfs = [s for s in paper.sources if s.media_kind is MediaKind.PDF]
    assert len(pdfs) == 2
    assert sum(1 for s in pdfs if s.is_primary) == 1
    primary = next(s for s in pdfs if s.is_primary)
    assert primary.rel_path == "LongContext.pdf"
    assert primary.role is SourceRole.ORIGINAL_PDF


def test_scan_never_binds_mineru_artefact_pdfs(vault: Path):
    """_layout/_origin/_span are render artefacts, never reader copies."""
    result = discover_papers(ScanConfig(root=vault))
    for paper in result.papers:
        for source in paper.sources:
            lowered = source.rel_path.lower()
            assert "_layout.pdf" not in lowered
            assert "_origin.pdf" not in lowered
            assert "_span.pdf" not in lowered


def test_scan_marks_completely_custom_markdown_as_other_role(vault: Path):
    result = discover_papers(ScanConfig(root=vault))
    paper = next(p for p in result.papers if p.display_title == "元上下文学习")
    roles = {s.role for s in paper.sources}
    assert SourceRole.OTHER_MARKDOWN in roles
    assert not any(r.is_translation for r in roles)
    # A custom-named markdown must not silently claim to be a translation.
    assert paper.binding_state is BindingState.PDF_ONLY


def test_scan_allows_pdf_only_paper(vault: Path):
    result = discover_papers(ScanConfig(root=vault))
    paper = next(p for p in result.papers if p.display_title == "SELF-REFINE")
    assert paper.binding_state is BindingState.PDF_ONLY
    assert len(paper.sources) == 1
    assert paper.primary_pdf_source_id is not None


def test_scan_never_emits_category_index_as_source(vault: Path):
    result = discover_papers(ScanConfig(root=vault))
    for paper in result.papers:
        for source in paper.sources:
            assert source.rel_path != "00-索引.md"


def test_scan_writes_nothing_to_the_vault(vault: Path):
    """Discovery is read-only: no manifest, no notes, no new files (ADR-006)."""
    before = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))
    discover_papers(ScanConfig(root=vault))
    after = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))
    assert before == after


def test_scan_ignores_non_pdf_extension_without_magic_bytes(tmp_path: Path):
    """A .pdf extension alone is not proof the file is a PDF."""
    root = tmp_path / "论文"
    d = root / "fake"
    d.mkdir(parents=True)
    (d / "fake.pdf").write_text("not a pdf at all", encoding="utf-8")
    result = discover_papers(ScanConfig(root=root))
    assert not result.papers


def test_scan_reports_missing_root(tmp_path: Path):
    result = discover_papers(ScanConfig(root=tmp_path / "nope"))
    assert result.errors
    assert not result.papers


def test_scan_is_deterministic(vault: Path):
    first = {p.folder_relpath for p in discover_papers(ScanConfig(root=vault)).papers}
    second = {p.folder_relpath for p in discover_papers(ScanConfig(root=vault)).papers}
    assert first == second


# ---------------------------------------------------------------------------
# Storage: schema, migration, authority split
# ---------------------------------------------------------------------------

def test_storage_creates_all_required_tables(tmp_path: Path):
    db = tmp_path / "papers.db"
    PaperStorage(db)
    conn = sqlite3.connect(db)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    conn.close()
    for expected in (
        "papers",
        "paper_sources",
        "paper_notes",
        "annotations_index",
        "workspace_states",
        "paper_write_intents",
        "paper_status_events",
    ):
        assert expected in tables, expected


def test_storage_records_schema_version(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    assert storage.schema_version == SCHEMA_VERSION


def test_storage_migration_is_idempotent(tmp_path: Path):
    db = tmp_path / "papers.db"
    PaperStorage(db).close()
    reopened = PaperStorage(db)
    assert reopened.schema_version == SCHEMA_VERSION
    assert reopened.count_papers() == 0


def test_storage_enables_wal_and_foreign_keys(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    with storage._lock:
        assert storage._conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
        assert storage._conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1


def test_storage_hardens_permissions(tmp_path: Path):
    db = tmp_path / "sub" / "papers.db"
    PaperStorage(db)
    assert oct(os.stat(db).st_mode)[-3:] == "600"
    assert oct(os.stat(db.parent).st_mode)[-3:] == "700"


def test_storage_backup_via_sqlite_api(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    storage.upsert_paper(
        Paper(paper_id=new_paper_id(), folder_relpath="a", display_title="A")
    )
    target = storage.backup_to(tmp_path / "backup.db")
    conn = sqlite3.connect(target)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
    conn.close()


def test_storage_rejects_source_for_unknown_paper(tmp_path: Path):
    """Foreign keys must stop orphan sources."""
    storage = PaperStorage(tmp_path / "papers.db")
    with pytest.raises(sqlite3.IntegrityError):
        storage.upsert_source(
            PaperSource(
                source_id=new_source_id(),
                paper_id="pw_missing",
                role=SourceRole.ORIGINAL_PDF,
                rel_path="x.pdf",
            )
        )


def test_storage_status_transition_logs_event(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    storage.set_status(pid, PaperStatus.READING, reason="first_load")
    events = storage.list_status_events(pid)
    assert len(events) == 1
    assert events[0]["to_status"] == "READING"
    assert events[0]["from_status"] == "UNREAD"


def test_storage_first_open_sets_first_opened_at_once(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    storage.set_status(pid, PaperStatus.READING)
    first = storage.get_paper(pid).first_opened_at
    assert first is not None
    storage.set_status(pid, PaperStatus.COMPLETED)
    storage.set_status(pid, PaperStatus.READING)
    assert storage.get_paper(pid).first_opened_at == first


def test_storage_completed_paper_is_not_downgraded_by_reopen(tmp_path: Path):
    """Re-opening a COMPLETED paper must not silently revert it (ADR-007)."""
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    storage.set_status(pid, PaperStatus.READING)
    storage.set_status(pid, PaperStatus.COMPLETED)
    storage.touch_last_opened(pid)
    assert storage.get_paper(pid).status is PaperStatus.COMPLETED


def test_storage_soft_delete_paper_keeps_row(tmp_path: Path):
    """ADR-002: removal is a marker, never a DELETE."""
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    storage.mark_paper_inactive(pid)
    assert storage.get_paper(pid).inactive_at is not None
    assert storage.count_papers() == 0
    assert len(storage.list_papers(include_inactive=True)) == 1


def test_storage_source_soft_deactivation(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    sid = new_source_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    storage.upsert_source(
        PaperSource(
            source_id=sid, paper_id=pid, role=SourceRole.ORIGINAL_PDF, rel_path="x.pdf"
        )
    )
    storage.deactivate_source(sid)
    assert storage.list_sources(pid) == []
    assert len(storage.list_sources(pid, include_inactive=True)) == 1


def test_storage_folder_uniqueness_blocks_duplicate_paper_in_same_folder(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    storage.upsert_paper(Paper(paper_id=new_paper_id(), folder_relpath="same", display_title="A"))
    with pytest.raises(sqlite3.IntegrityError):
        storage.upsert_paper(
            Paper(paper_id=new_paper_id(), folder_relpath="same", display_title="B")
        )


def test_storage_write_intent_lifecycle(tmp_path: Path):
    """Multi-file writes roll forward from an intent record (ADR-007)."""
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    intent = storage.begin_write_intent(pid, "create_note", {"rel_path": "notes.md"})
    assert len(storage.list_pending_write_intents()) == 1
    storage.commit_write_intent(intent)
    assert storage.list_pending_write_intents() == []


def test_storage_workspace_state_round_trip(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    state = WorkspaceState(
        paper_id=pid,
        source_positions={"src_1": {"page_index": 7, "page_offset_ratio": 0.25}},
        note_cursor_start=10,
        note_cursor_end=20,
    )
    storage.upsert_workspace_state(state)
    loaded = storage.get_workspace_state(pid)
    assert loaded.source_positions["src_1"]["page_index"] == 7
    assert loaded.note_cursor_start == 10


def test_storage_note_round_trip(tmp_path: Path):
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    nid = new_note_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    storage.upsert_note(
        PaperNote(note_id=nid, paper_id=pid, rel_path="notes.md", note_tags=["x"])
    )
    assert storage.get_note_for_paper(pid).note_tags == ["x"]


def test_storage_annotation_index_is_replaceable(tmp_path: Path):
    """The index is derived; it must be safe to rebuild from the sidecar."""
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    row = {
        "annotation_id": "ann_1",
        "source_id": "src_1",
        "kind": "HIGHLIGHT",
        "anchor_type": "PDF_TEXT",
        "page_index": 3,
        "source_sha256": "a" * 64,
        "created_at": "2026-09-16T00:00:00Z",
        "updated_at": "2026-09-16T00:00:00Z",
    }
    storage.replace_annotations_index(pid, [row])
    assert len(storage.list_annotations(pid)) == 1
    storage.replace_annotations_index(pid, [])
    assert storage.list_annotations(pid) == []


# ---------------------------------------------------------------------------
# Identity stability: the core acceptance gate from ADR-006
# ---------------------------------------------------------------------------

def test_copying_a_paper_folder_fails_closed(tmp_path: Path):
    """ADR-006: a copied folder carries its manifest, so the same paper_id
    appears twice. The system must NOT silently pick a winner."""
    storage = PaperStorage(tmp_path / "papers.db")
    storage.upsert_paper(
        Paper(paper_id="pw_dup", folder_relpath="论文/原始", display_title="A")
    )
    written = storage.upsert_paper(
        Paper(paper_id="pw_dup", folder_relpath="论文/副本", display_title="A")
    )
    assert written is False
    paper = storage.get_paper("pw_dup")
    assert paper.binding_state is BindingState.DUPLICATE_ID_CONFLICT
    assert paper.folder_relpath == "论文/原始", "原始绑定不得被副本覆盖"


def test_moving_a_paper_folder_preserves_identity_and_status(tmp_path: Path):
    """The whole point of a path-independent ID: rename/move must be survivable."""
    storage = PaperStorage(tmp_path / "papers.db")
    storage.upsert_paper(
        Paper(paper_id="pw_mv", folder_relpath="论文/04-Harness/旧名", display_title="X")
    )
    storage.set_status("pw_mv", PaperStatus.READING)
    first_opened = storage.get_paper("pw_mv").first_opened_at

    storage.relocate_paper("pw_mv", "论文/02-上下文/新名", "论文/02-上下文")

    moved = storage.get_paper("pw_mv")
    assert moved.folder_relpath == "论文/02-上下文/新名"
    assert moved.status is PaperStatus.READING
    assert moved.first_opened_at == first_opened


def test_renamed_source_file_keeps_source_identity(tmp_path: Path):
    """Repository-level rename recovery uses source_id/sha256, never path (ADR-006)."""
    storage = PaperStorage(tmp_path / "papers.db")
    pid = new_paper_id()
    sid = new_source_id()
    storage.upsert_paper(Paper(paper_id=pid, folder_relpath="a", display_title="A"))
    storage.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="old name.pdf",
            sha256="a" * 64,
        )
    )
    # Same bytes re-registered under a new filename.
    storage.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="new name.pdf",
            sha256="a" * 64,
        )
    )
    sources = storage.list_sources(pid)
    assert len(sources) == 1, "renaming must not create a second source"
    assert sources[0].rel_path == "new name.pdf"
    assert sources[0].source_id == sid


def test_content_hash_is_not_an_identity(tmp_path: Path):
    """Two papers may legitimately share bytes; the hash must not merge them."""
    storage = PaperStorage(tmp_path / "papers.db")
    shared = "b" * 64
    for index, folder in enumerate(("论文/方向A/同名", "论文/方向B/同名")):
        pid = new_paper_id()
        storage.upsert_paper(
            Paper(paper_id=pid, folder_relpath=folder, display_title=f"P{index}")
        )
        storage.upsert_source(
            PaperSource(
                source_id=new_source_id(),
                paper_id=pid,
                role=SourceRole.ORIGINAL_PDF,
                rel_path="same.pdf",
                sha256=shared,
            )
        )
    hits = storage.find_sources_by_sha(shared)
    assert len(hits) == 2
    assert hits[0].paper_id != hits[1].paper_id


def test_deleting_source_row_is_not_exposed(tmp_path: Path):
    """ADR-002: no physical delete API may exist for sources."""
    storage = PaperStorage(tmp_path / "papers.db")
    assert not hasattr(storage, "delete_source")
    assert not hasattr(storage, "delete_paper")
    assert not hasattr(storage, "delete_note")
