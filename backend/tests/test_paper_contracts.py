"""Contract tests for the frozen Paper Workbench schemas (ADR-006..009).

These tests guard the *irreversible* decisions. They are written as adversarial
assertions: each one states the failure mode that would force a P1 rewrite if it
were ever allowed to pass.
"""

from __future__ import annotations

import copy

import pytest

from backend.app.paper.contracts import (
    SchemaError,
    validate_ai_context,
    validate_annotations,
    validate_manifest,
)

PAPER_ID = "pw_3d9e1234-5678-4abc-89de-0123456789ab"
PDF_SRC = "src_a12f1234-5678-4abc-89de-0123456789ab"
FULL_SRC = "src_c9081234-5678-4abc-89de-0123456789ab"
NOTE_ID = "note_f8cd1234-5678-4abc-89de-0123456789ab"
ANN_ID = "ann_11111111-2222-4333-8999-444444444444"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "paper_id": PAPER_ID,
        "title_override": None,
        "sources": [
            {
                "source_id": PDF_SRC,
                "role": "ORIGINAL_PDF",
                "path": "Agent橙皮书.pdf",
                "primary": True,
                "active": True,
            },
            {
                "source_id": FULL_SRC,
                "role": "TRANSLATION_FULL",
                "path": "Agent橙皮书_全文翻译.md",
                "primary": True,
                "active": True,
            },
        ],
        "note": {"note_id": NOTE_ID, "path": "notes.md"},
        "annotation_store": "paper.annotations.json",
        "tags": ["Transformer"],
        "created_at": "2026-09-16T00:00:00Z",
        "updated_at": "2026-09-16T00:00:00Z",
        "inactive_at": None,
    }


def _annotations() -> dict:
    return {
        "schema_version": 1,
        "paper_id": PAPER_ID,
        "annotations": [
            {
                "annotation_id": ANN_ID,
                "source_id": PDF_SRC,
                "kind": "HIGHLIGHT",
                "body_markdown": "",
                "selected_text": "self-attention",
                "anchor_schema_version": 1,
                "anchor": {
                    "type": "PDF_TEXT",
                    "page_index": 0,
                    "page_label": "1",
                    "rotation": 0,
                    "quad_points_normalized": [
                        {"x": 0.1, "y": 0.2},
                        {"x": 0.4, "y": 0.2},
                    ],
                    "text_quote": {
                        "exact": "self-attention",
                        "prefix": "the",
                        "suffix": "layer",
                    },
                },
                "source_sha256": SHA_A,
                "source_version": 3,
                "created_at": "2026-09-16T00:00:00Z",
                "updated_at": "2026-09-16T00:00:00Z",
                "deleted_at": None,
                "orphaned_at": None,
                "revision": 1,
            }
        ],
    }


def _ai_context() -> dict:
    return {
        "schema_version": "1",
        "assembled_at": "2026-09-16T00:00:00Z",
        "paper": {
            "paper_id": PAPER_ID,
            "title": "Attention Is All You Need",
            "tags": ["Transformer"],
            "status": "READING",
        },
        "focus": {
            "pane": "PDF",
            "source_id": PDF_SRC,
            "source_version": 3,
            "source_sha256": SHA_A,
            "locator": {"page_index": 7},
            "selection": {"exact": "scaled dot-product attention"},
        },
        "source_refs": [
            {
                "source_id": PDF_SRC,
                "role": "ORIGINAL_PDF",
                "sha256": SHA_B,
                "excerpt": "...",
            }
        ],
        "note": {"note_id": NOTE_ID, "sha256": SHA_C, "content": "# 笔记"},
        "annotations": [
            {
                "annotation_id": ANN_ID,
                "source_id": PDF_SRC,
                "kind": "QUESTION",
                "anchor": {"type": "PDF_TEXT"},
                "body_markdown": "为什么需要缩放？",
                "selected_text": None,
            }
        ],
        "resource_versions": [
            {"resource_id": PDF_SRC, "source_version": 3, "sha256": SHA_B}
        ],
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_manifest_valid_document_accepted():
    validate_manifest(_manifest())


def test_manifest_allows_multiple_sources_per_paper():
    """ADR-006: Paper 1:N PaperSource. A second PDF must not be rejected.

    Regression guard: the original requirement modelled pdfPath as a scalar,
    which silently drops the 85 MinerU-nested papers that carry extra PDFs.
    """
    doc = _manifest()
    doc["sources"].append(
        {
            "source_id": "src_bbbb1234-5678-4abc-89de-0123456789ab",
            "role": "SUPPLEMENTAL_PDF",
            "path": "appendix.pdf",
            "primary": False,
            "active": True,
        }
    )
    validate_manifest(doc)


def test_manifest_allows_pdf_only_paper():
    """ADR-006: minimum legal condition is 'at least one readable source'."""
    doc = _manifest()
    doc["sources"] = doc["sources"][:1]
    doc["note"] = None
    doc.pop("annotation_store")
    validate_manifest(doc)


def test_manifest_keeps_all_translation_roles_distinct():
    """Guide, full translation and MinerU extraction must stay separate roles."""
    doc = _manifest()
    doc["sources"] = [
        doc["sources"][0],
        {
            "source_id": FULL_SRC,
            "role": "TRANSLATION_GUIDE",
            "path": "x_翻译导读.md",
            "primary": False,
            "active": True,
        },
        {
            "source_id": "src_dddd1234-5678-4abc-89de-0123456789ab",
            "role": "TRANSLATION_FULL",
            "path": "x_全文翻译.md",
            "primary": True,
            "active": True,
        },
        {
            "source_id": "src_eeee1234-5678-4abc-89de-0123456789ab",
            "role": "EXTRACTED_MARKDOWN",
            "path": "x/full.md",
            "primary": False,
            "active": True,
        },
        {
            "source_id": "src_ffff1234-5678-4abc-89de-0123456789ab",
            "role": "OTHER_MARKDOWN",
            "path": "元上下文学习.md",
            "primary": False,
            "active": True,
        },
    ]
    validate_manifest(doc)


def test_manifest_rejects_path_traversal():
    """Relative bindings must not escape the paper folder (ADR-006)."""
    doc = _manifest()
    doc["sources"][0]["path"] = "../escape.pdf"
    with pytest.raises(SchemaError):
        validate_manifest(doc)


def test_manifest_rejects_absolute_path():
    doc = _manifest()
    doc["sources"][0]["path"] = "/etc/passwd"
    with pytest.raises(SchemaError):
        validate_manifest(doc)


def test_manifest_rejects_windows_absolute_path():
    doc = _manifest()
    doc["sources"][0]["path"] = "C:\\Users\\escape.pdf"
    with pytest.raises(SchemaError):
        validate_manifest(doc)


def test_manifest_rejects_path_derived_identity():
    """ADR-006: paper_id must be a random UUID, never derived from the folder name."""
    doc = _manifest()
    doc["paper_id"] = "pw_attention-is-all-you-need"
    with pytest.raises(SchemaError):
        validate_manifest(doc)


def test_manifest_rejects_unsigned_uuid_as_paper_id():
    """A dashed-but-non-v4 token must not sneak through as a stable identity."""
    doc = _manifest()
    doc["paper_id"] = "pw_" + "0" * 32
    with pytest.raises(SchemaError):
        validate_manifest(doc)


def test_manifest_rejects_unknown_role():
    doc = _manifest()
    doc["sources"][0]["role"] = "TRANSLATION"
    with pytest.raises(SchemaError):
        validate_manifest(doc)


def test_manifest_rejects_unknown_field():
    """additionalProperties:false keeps the frozen contract from drifting."""
    doc = _manifest()
    doc["status"] = "reading"
    with pytest.raises(SchemaError):
        validate_manifest(doc)


def test_manifest_soft_delete_marker_accepted():
    """ADR-002: removal is a marker, never a deletion."""
    doc = _manifest()
    doc["inactive_at"] = "2026-09-16T01:00:00Z"
    validate_manifest(doc)


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

def test_annotations_valid_pdf_anchor():
    validate_annotations(_annotations())


def test_annotations_valid_markdown_anchor():
    doc = _annotations()
    doc["annotations"][0]["anchor"] = {
        "type": "MARKDOWN_TEXT",
        "heading_path": ["3 Method", "3.2 Training"],
        "block_fingerprint": "abc123",
        "text_position": {"start": 318, "end": 371},
        "text_quote": {"exact": "..."},
    }
    validate_annotations(doc)


def test_annotations_require_normalized_coordinates():
    """ADR-008: CSS pixels are forbidden — they break on zoom and DPI change."""
    doc = _annotations()
    doc["annotations"][0]["anchor"].pop("quad_points_normalized")
    with pytest.raises(SchemaError):
        validate_annotations(doc)


def test_annotations_reject_out_of_range_coordinates():
    doc = _annotations()
    doc["annotations"][0]["anchor"]["quad_points_normalized"] = [{"x": 1.5, "y": 0.2}]
    with pytest.raises(SchemaError):
        validate_annotations(doc)


def test_annotations_require_text_quote_fallback():
    """Without a text quote a reflowed document can never be re-anchored."""
    doc = _annotations()
    doc["annotations"][0]["anchor"].pop("text_quote")
    with pytest.raises(SchemaError):
        validate_annotations(doc)


def test_annotations_require_source_hash_for_orphan_detection():
    """ADR-008: a missing hash makes silent mis-anchoring undetectable."""
    doc = _annotations()
    doc["annotations"][0].pop("source_sha256")
    with pytest.raises(SchemaError):
        validate_annotations(doc)


def test_annotations_require_source_version():
    doc = _annotations()
    doc["annotations"][0].pop("source_version")
    with pytest.raises(SchemaError):
        validate_annotations(doc)


def test_annotations_reject_negative_page_index():
    """page_index is 0-based and cannot be negative."""
    doc = _annotations()
    doc["annotations"][0]["anchor"]["page_index"] = -1
    with pytest.raises(SchemaError):
        validate_annotations(doc)


def test_annotations_reject_non_hex_sha256():
    doc = _annotations()
    doc["annotations"][0]["source_sha256"] = "z" * 64
    with pytest.raises(SchemaError):
        validate_annotations(doc)


def test_annotations_soft_delete_via_deleted_at():
    """ADR-002/008: deletion is a timestamp, the record stays in the array."""
    doc = _annotations()
    doc["annotations"][0]["deleted_at"] = "2026-09-16T02:00:00Z"
    validate_annotations(doc)
    assert len(doc["annotations"]) == 1


def test_annotations_orphan_marker_accepted():
    doc = _annotations()
    doc["annotations"][0]["orphaned_at"] = "2026-09-16T02:00:00Z"
    validate_annotations(doc)


def test_annotations_accept_all_declared_kinds():
    for kind in (
        "HIGHLIGHT",
        "COMMENT",
        "THOUGHT",
        "INNOVATION",
        "QUESTION",
        "CONCLUSION",
    ):
        doc = _annotations()
        doc["annotations"][0]["kind"] = kind
        validate_annotations(doc)


def test_annotations_reject_unknown_kind():
    doc = _annotations()
    doc["annotations"][0]["kind"] = "BOOKMARK"
    with pytest.raises(SchemaError):
        validate_annotations(doc)


# ---------------------------------------------------------------------------
# AIContextV1 (P1 interface frozen in P0)
# ---------------------------------------------------------------------------

def test_ai_context_valid_envelope():
    validate_ai_context(_ai_context())


def test_ai_context_allows_focus_without_selection():
    """Opening a paper without a selection must still produce a valid context."""
    doc = _ai_context()
    doc["focus"] = {"pane": "NOTE", "source_id": PDF_SRC}
    validate_ai_context(doc)


def test_ai_context_allows_null_focus_and_note():
    doc = _ai_context()
    doc["focus"] = None
    doc["note"] = None
    validate_ai_context(doc)


def test_ai_context_rejects_invalid_status():
    doc = _ai_context()
    doc["paper"]["status"] = "READ"
    with pytest.raises(SchemaError):
        validate_ai_context(doc)


def test_ai_context_rejects_invalid_pane():
    doc = _ai_context()
    doc["focus"]["pane"] = "TRANSLATION"
    with pytest.raises(SchemaError):
        validate_ai_context(doc)


def test_ai_context_requires_resource_versions():
    """Traceability: every context must stamp the exact bytes it was built from."""
    doc = _ai_context()
    doc.pop("resource_versions")
    with pytest.raises(SchemaError):
        validate_ai_context(doc)


def test_ai_context_requires_assembled_at():
    doc = _ai_context()
    doc.pop("assembled_at")
    with pytest.raises(SchemaError):
        validate_ai_context(doc)


def test_ai_context_rejects_bad_timestamp():
    doc = _ai_context()
    doc["assembled_at"] = "yesterday"
    with pytest.raises(SchemaError):
        validate_ai_context(doc)


def test_ai_context_versions_are_not_mutated_by_validation():
    """Validation must be read-only so it can run on live aggregates."""
    doc = _ai_context()
    snapshot = copy.deepcopy(doc)
    validate_ai_context(doc)
    assert doc == snapshot
