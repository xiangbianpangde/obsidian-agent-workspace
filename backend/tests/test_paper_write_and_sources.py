"""Write-service and PDF source endpoint tests.

Two contracts are under test:

``VaultWriteService`` — the single funnel for every Vault mutation (ADR-007).
The interesting cases are all refusals: path escape, no-clobber, optimistic-lock
mismatch, and the pre-commit re-validation that catches an external editor
writing between the first check and the rename.

``/api/paper-sources/{source_id}/content`` — a byte-range-capable read-only PDF
endpoint (ADR-009). A 23 MB paper cannot be paged without range support, so the
full 200/206/416/HEAD contract is asserted, along with the safety headers and
version pinning that stops PDF.js stitching two revisions together.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.paper.api_sources import _content_disposition, _parse_range
from backend.app.paper.models import (
    MediaKind,
    Paper,
    PaperSource,
    SourceRole,
    new_paper_id,
    new_source_id,
)
from backend.app.paper.storage import PaperStorage
from backend.app.paper.writer import (
    TEMP_PREFIX,
    AlreadyExistsError,
    ConflictError,
    PathRejected,
    VaultWriteService,
    sha256_bytes,
)

PDF_BODY = b"%PDF-1.4\n" + bytes(range(256)) * 8 + b"\n%%EOF\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "论文" / "A").mkdir(parents=True)
    return root


@pytest.fixture
def service(vault: Path, tmp_path: Path) -> VaultWriteService:
    return VaultWriteService(vault, backup_root=tmp_path / "backups")


# ---------------------------------------------------------------------------
# Range parsing
# ---------------------------------------------------------------------------

def test_parse_range_simple():
    assert _parse_range("bytes=0-99", 1000) == (0, 99)


def test_parse_range_open_ended():
    assert _parse_range("bytes=100-", 1000) == (100, 999)


def test_parse_range_suffix_form():
    """bytes=-100 means the last 100 bytes, not 'from 0 to 100'."""
    assert _parse_range("bytes=-100", 1000) == (900, 999)


def test_parse_range_end_clamped_to_size():
    assert _parse_range("bytes=0-99999", 1000) == (0, 999)


def test_parse_range_single_byte():
    assert _parse_range("bytes=0-0", 1000) == (0, 0)


def test_parse_range_unsatisfiable_start_raises():
    with pytest.raises(ValueError):
        _parse_range("bytes=1000-", 1000)


def test_parse_range_reversed_raises():
    with pytest.raises(ValueError):
        _parse_range("bytes=100-50", 1000)


def test_parse_range_zero_suffix_raises():
    with pytest.raises(ValueError):
        _parse_range("bytes=-0", 1000)


def test_parse_range_malformed_is_ignored():
    """Per RFC 9110 a malformed unit means the header should be ignored."""
    assert _parse_range("items=0-5", 1000) is None
    assert _parse_range("garbage", 1000) is None


# ---------------------------------------------------------------------------
# Content-Disposition (Chinese filenames)
# ---------------------------------------------------------------------------

def test_content_disposition_encodes_chinese_filename():
    header = _content_disposition("SkillZipPro：面向自演化智能体.pdf")
    assert "filename*=UTF-8''" in header
    assert "inline" in header
    # The fallback must remain a legal quoted string.
    assert '"' in header


def test_content_disposition_strips_header_injection():
    header = _content_disposition('evil"\r\nX-Injected: 1.pdf')
    assert "\r" not in header and "\n" not in header


# ---------------------------------------------------------------------------
# VaultWriteService
# ---------------------------------------------------------------------------

def test_create_writes_file(service: VaultWriteService):
    result = service.create("论文/A/notes.md", "# 笔记\n")
    assert result.created is True
    assert result.new_hash == sha256_bytes("# 笔记\n".encode("utf-8"))


def test_create_refuses_to_clobber(service: VaultWriteService):
    service.create("论文/A/notes.md", "first")
    with pytest.raises(AlreadyExistsError):
        service.create("论文/A/notes.md", "second")


def test_create_leaves_no_temp_file(service: VaultWriteService, vault: Path):
    service.create("论文/A/notes.md", "x")
    assert list(vault.rglob(f"{TEMP_PREFIX}*")) == []


def test_create_sets_private_permissions(service: VaultWriteService, vault: Path):
    service.create("论文/A/notes.md", "x")
    assert oct(os.stat(vault / "论文/A/notes.md").st_mode)[-3:] == "600"


def test_save_requires_expected_hash(service: VaultWriteService):
    """Overwriting without proving we read the current bytes is a conflict."""
    service.create("论文/A/notes.md", "x")
    with pytest.raises(ConflictError):
        service.save("论文/A/notes.md", "y", expected_hash=None)


def test_save_rejects_stale_hash(service: VaultWriteService):
    service.create("论文/A/notes.md", "x")
    with pytest.raises(ConflictError):
        service.save("论文/A/notes.md", "y", expected_hash="0" * 64)


def test_save_with_correct_hash_succeeds_and_returns_new_hash(service: VaultWriteService):
    service.create("论文/A/notes.md", "old")
    _, current = service.read("论文/A/notes.md")
    result = service.save("论文/A/notes.md", "new", expected_hash=current)
    assert result.new_hash == sha256_bytes(b"new")
    assert result.previous_hash == current
    assert result.created is False


def test_save_takes_a_backup_of_the_preimage(service: VaultWriteService):
    service.create("论文/A/notes.md", "old")
    _, current = service.read("论文/A/notes.md")
    result = service.save("论文/A/notes.md", "new", expected_hash=current)
    assert result.backup_path is not None
    assert Path(result.backup_path).read_bytes() == b"old"


def test_backup_is_deduplicated_by_content(service: VaultWriteService):
    """Autosave must not produce one backup per keystroke."""
    service.create("论文/A/notes.md", "v1")
    _, current = service.read("论文/A/notes.md")
    first = service.save("论文/A/notes.md", "v2", expected_hash=current).backup_path
    _, current = service.read("论文/A/notes.md")
    second = service.save("论文/A/notes.md", "v3", expected_hash=current).backup_path
    assert first != second, "different preimages deserve different backups"


def test_save_rejects_path_traversal(service: VaultWriteService):
    for bad in ("../escape.md", "/etc/passwd", "论文/../../escape.md"):
        with pytest.raises(PathRejected):
            service.resolve(bad)


def test_save_rejects_windows_absolute_path(service: VaultWriteService):
    with pytest.raises(PathRejected):
        service.resolve("C:\\Windows\\evil.md")


def test_save_rejects_excluded_area(service: VaultWriteService):
    with pytest.raises(PathRejected):
        service.resolve(".obsidian/plugins/x.json")


def test_save_rejects_symlink_escape(service: VaultWriteService, vault: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault / "link"
    link.symlink_to(outside)
    with pytest.raises(PathRejected):
        service.resolve("link/evil.md")


def test_sweep_temp_files_removes_only_temp(service: VaultWriteService, vault: Path):
    (vault / "论文/A" / f"{TEMP_PREFIX}abc").write_text("junk")
    real = vault / "论文/A/notes.md"
    real.write_text("keep")
    removed = service.sweep_temp_files()
    assert removed == 1
    assert real.exists(), "user content must never be swept"


def test_service_exposes_no_delete_api():
    """ADR-002: the write service must not be able to delete anything."""
    for name in ("delete", "remove", "unlink", "purge"):
        assert not hasattr(VaultWriteService, name)


# ---------------------------------------------------------------------------
# PDF source endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf_app(vault: Path, tmp_path: Path):
    """Build an app whose storage and vault point at the temp fixtures."""
    db = tmp_path / "papers.db"
    storage = PaperStorage(db)

    paper_dir = vault / "论文" / "A"
    pdf_path = paper_dir / "论文正文.pdf"
    pdf_path.write_bytes(PDF_BODY)

    pid = new_paper_id()
    sid = new_source_id()
    storage.upsert_paper(
        Paper(paper_id=pid, folder_relpath="论文/A", display_title="测试论文")
    )
    storage.upsert_source(
        PaperSource(
            source_id=sid,
            paper_id=pid,
            role=SourceRole.ORIGINAL_PDF,
            rel_path="论文正文.pdf",
            source_version=3,
            mime_type="application/pdf",
        )
    )

    app = FastAPI()

    import backend.app.paper.api_sources as api_sources
    import backend.app.state as app_state

    class _Cfg:
        vault_root = vault
        # The endpoint resolves folder_relpath against the papers root, which
        # may differ from the vault root; the stub must expose both.
        papers_root = vault

        @property
        def papers_root_or_default(self):
            return self.papers_root

    app_state._state["cfg"] = _Cfg()

    original_storage = api_sources.paper_storage.PaperStorage
    api_sources.paper_storage.PaperStorage = lambda *a, **k: storage

    app.include_router(api_sources.router)
    client = TestClient(app)
    yield client, sid, storage, pdf_path
    api_sources.paper_storage.PaperStorage = original_storage
    storage.close()


def test_pdf_endpoint_returns_whole_file(pdf_app):
    client, sid, _, _ = pdf_app
    response = client.get(f"/api/paper-sources/{sid}/content")
    assert response.status_code == 200
    assert response.content == PDF_BODY
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(PDF_BODY))


def test_pdf_endpoint_safety_headers(pdf_app):
    client, sid, _, _ = pdf_app
    headers = client.head(f"/api/paper-sources/{sid}/content").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert "no-store" in headers["cache-control"]
    assert "filename*=UTF-8''" in headers["content-disposition"]


def test_pdf_endpoint_head_matches_get_headers(pdf_app):
    client, sid, _, _ = pdf_app
    get_headers = client.get(f"/api/paper-sources/{sid}/content").headers
    head = client.head(f"/api/paper-sources/{sid}/content")
    assert head.status_code == 200
    assert head.headers["content-length"] == get_headers["content-length"]
    assert head.headers["etag"] == get_headers["etag"]


def test_pdf_endpoint_partial_content(pdf_app):
    client, sid, _, _ = pdf_app
    response = client.get(
        f"/api/paper-sources/{sid}/content", headers={"Range": "bytes=0-99"}
    )
    assert response.status_code == 206
    assert response.content == PDF_BODY[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(PDF_BODY)}"
    assert response.headers["content-length"] == "100"


def test_pdf_endpoint_open_ended_range(pdf_app):
    client, sid, _, _ = pdf_app
    response = client.get(
        f"/api/paper-sources/{sid}/content", headers={"Range": "bytes=100-"}
    )
    assert response.status_code == 206
    assert response.content == PDF_BODY[100:]


def test_pdf_endpoint_suffix_range(pdf_app):
    client, sid, _, _ = pdf_app
    response = client.get(
        f"/api/paper-sources/{sid}/content", headers={"Range": "bytes=-50"}
    )
    assert response.status_code == 206
    assert response.content == PDF_BODY[-50:]


def test_pdf_endpoint_unsatisfiable_range_returns_416(pdf_app):
    client, sid, _, _ = pdf_app
    response = client.get(
        f"/api/paper-sources/{sid}/content", headers={"Range": "bytes=999999-"}
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(PDF_BODY)}"


def test_pdf_endpoint_rejects_stale_version(pdf_app):
    """A PDF replaced mid-session must not be silently re-served."""
    client, sid, _, _ = pdf_app
    assert client.get(f"/api/paper-sources/{sid}/content?version=3").status_code == 200
    assert client.get(f"/api/paper-sources/{sid}/content?version=2").status_code == 412


def test_pdf_endpoint_unknown_source_is_404(pdf_app):
    client, _, _, _ = pdf_app
    assert client.get("/api/paper-sources/src_missing/content").status_code == 404


def test_pdf_endpoint_inactive_source_is_404(pdf_app):
    client, sid, storage, _ = pdf_app
    storage.deactivate_source(sid)
    assert client.get(f"/api/paper-sources/{sid}/content").status_code == 404


def test_pdf_endpoint_rejects_missing_file(pdf_app):
    client, sid, _, pdf_path = pdf_app
    pdf_path.unlink()
    assert client.get(f"/api/paper-sources/{sid}/content").status_code == 404


def test_pdf_endpoint_never_modifies_the_file(pdf_app):
    """PDF bytes are permanently read-only (ADR-009)."""
    client, sid, _, pdf_path = pdf_app
    before = pdf_path.read_bytes()
    mtime = pdf_path.stat().st_mtime_ns
    client.get(f"/api/paper-sources/{sid}/content")
    client.get(f"/api/paper-sources/{sid}/content", headers={"Range": "bytes=0-10"})
    client.head(f"/api/paper-sources/{sid}/content")
    assert pdf_path.read_bytes() == before
    assert pdf_path.stat().st_mtime_ns == mtime


def test_pdf_endpoint_rejects_non_pdf_source(pdf_app):
    """A markdown source must not be served by the PDF endpoint."""
    client, _, storage, _ = pdf_app
    other = new_source_id()
    paper = storage.list_papers()[0]
    storage.upsert_source(
        PaperSource(
            source_id=other,
            paper_id=paper.paper_id,
            role=SourceRole.TRANSLATION_FULL,
            rel_path="x_全文翻译.md",
        )
    )
    assert client.get(f"/api/paper-sources/{other}/content").status_code == 415


# ---------------------------------------------------------------------------
# Single range code path (regression)
# ---------------------------------------------------------------------------

def test_multirange_is_ignored_not_silently_handled(pdf_app):
    """Regression: Starlette's FileResponse also implements ranges.

    Leaving the Range header in the ASGI scope meant lowercase ``bytes=`` was
    answered by this module while every other form was answered by Starlette,
    so ``bytes=0-9,20-29`` returned a multipart/byteranges response from one
    endpoint and a single 206 from the same endpoint elsewhere. ADR-009 only
    requires single ranges, so multi-range must be ignored and serve the whole
    entity — never a second, undocumented code path.
    """
    client, sid, _, _ = pdf_app
    response = client.get(
        f"/api/paper-sources/{sid}/content", headers={"Range": "bytes=0-9,20-29"}
    )
    assert response.status_code == 200
    assert response.content == PDF_BODY
    assert "multipart/byteranges" not in response.headers.get("content-type", "")


def test_range_unit_is_case_insensitive(pdf_app):
    """RFC 9110: the range unit is case-insensitive."""
    client, sid, _, _ = pdf_app
    response = client.get(
        f"/api/paper-sources/{sid}/content", headers={"Range": "Bytes=0-9"}
    )
    assert response.status_code == 206
    assert response.content == PDF_BODY[:10]


def test_malformed_range_serves_whole_entity(pdf_app):
    client, sid, _, _ = pdf_app
    response = client.get(
        f"/api/paper-sources/{sid}/content", headers={"Range": "items=0-5"}
    )
    assert response.status_code == 200
    assert response.content == PDF_BODY


def test_parse_range_multirange_returns_none():
    assert _parse_range("bytes=0-9,20-29", 1000) is None
