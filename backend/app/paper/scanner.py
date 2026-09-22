"""Paper discovery scanner.

Discovers papers under a configured vault root and binds their sources, writing
**only** to the SQLite index. Adoption (writing an in-folder manifest) happens
later, when a paper first acquires dependent state (ADR-006).

The real vault this targets is far messier than the original requirements
assumed. Observed shapes:

    02. 🟡 归类 Arrange/论文/04-Harness执行框架/SkillZipPro：.../
      ├── SkillZipPro：....pdf                     <- top-level, the reader
      ├── SkillZipPro：..._翻译导读.md
      ├── SkillZipPro：..._全文翻译.md
      └── SkillZipPro：.../                        <- MinerU artifact container
          ├── full.md
          ├── <uuid>_origin.pdf
          ├── <uuid>_layout.pdf                     <- 23MB, never the main PDF
          └── <uuid>_content_list.json

Binding rules therefore optimise for **precision over recall**: an automatic
binding only happens when it is unambiguous. Everything else becomes
``AMBIGUOUS`` and waits for a human. Guessing wrong is far worse than not
binding, because a wrong binding silently attaches the wrong file to a paper.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_mod
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import MANIFEST_FILENAME
from .manifest import manifest_to_sources, parse_manifest
from .models import (
    BindingOrigin,
    BindingState,
    MediaKind,
    Paper,
    PaperSource,
    SourceRole,
    new_source_id,
    utc_now,
)

__all__ = [
    "ScanConfig",
    "ScanResult",
    "discover_papers",
    "classify_markdown_role",
    "is_mineru_artifact_dir",
    "is_valid_utf8_text",
    "normalize_title",
]

#: Markdown filenames that are never a paper source. Matched case-insensitively.
_EXCLUDED_MARKDOWN_NAMES = {"00-索引.md", "index.md", "readme.md"}

#: MinerU artifact directory markers. Several must be present to classify.
#: ``_layout.pdf`` / ``_origin.pdf`` / ``_span.pdf`` are render artefacts, never
#: the reader copy.
_MINERU_MARKERS = (
    "_origin.pdf",
    "_layout.pdf",
    "_span.pdf",
    "_content_list.json",
    "_content_list_v2.json",
    "_model.json",
    "_middle.json",
)

_LAYOUT_RE = re.compile(r"_(layout|origin|span)\.pdf$", re.IGNORECASE)
_ANY_PDF_RE = re.compile(r"\.pdf$", re.IGNORECASE)

_PDF_SUFFIX = ".pdf"
_MD_SUFFIX = ".md"


@dataclass
class ScanConfig:
    """Scanner configuration. ``max_depth`` is an anomaly guard, not a rule."""

    root: Path
    max_depth: int = 6
    #: Directories pruned before descent, matched on name.
    exclude_dirs: Sequence[str] = (".obsidian", ".trash", "_attachments", "附件")
    #: Skip descending into MinerU artifact containers as paper candidates.
    detect_mineru: bool = True


@dataclass
class ScanResult:
    """Outcome of one scan. Purely read-only w.r.t. the Vault."""

    papers: List[Paper] = field(default_factory=list)
    ambiguous: List[Paper] = field(default_factory=list)
    mineru_containers: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def scanned_count(self) -> int:
        return len(self.papers) + len(self.ambiguous)


def normalize_title(value: str) -> str:
    """Normalise a folder or file stem for comparison.

    Handles the messy reality of these names: Unicode NFC/NFD differences
    (macOS stores NFD), full-width punctuation, and decorative suffixes.
    """
    text = unicodedata.normalize("NFC", value)
    # Full-width colon/comma/parenthesis -> ASCII equivalents.
    for src, dst in (("：", ":"), ("，", ","), ("（", "("), ("）", ")"), ("－", "-")):
        text = text.replace(src, dst)
    # Strip trailing role suffixes and whitespace.
    text = re.sub(r"_(翻译导读|全文翻译|中文翻译)$", "", text)
    text = text.replace(" ", "").replace("\u3000", "")
    return text.strip().lower()


def classify_markdown_role(filename: str) -> Optional[SourceRole]:
    """Map a markdown filename to a source role, or ``None`` if not a source.

    Kept deliberately strict: an unrecognised name becomes ``OTHER_MARKDOWN``
    rather than being promoted to a translation role.
    """
    name = filename.strip()
    if name.lower() in _EXCLUDED_MARKDOWN_NAMES:
        return None
    if name == "full.md":
        # MinerU extraction. Must not be presented as a Chinese translation.
        return SourceRole.EXTRACTED_MARKDOWN
    stem = name[: -len(_MD_SUFFIX)] if name.lower().endswith(_MD_SUFFIX) else name
    if stem.endswith("_全文翻译") or stem.endswith("_全文譯"):
        return SourceRole.TRANSLATION_FULL
    if stem.endswith("_翻译导读") or stem.endswith("_翻譯導讀"):
        return SourceRole.TRANSLATION_GUIDE
    if stem.endswith("_中文翻译"):
        return SourceRole.TRANSLATION_FULL
    return SourceRole.OTHER_MARKDOWN


def is_mineru_artifact_dir(path: Path) -> bool:
    """True when a directory looks like a MinerU parse-output container.

    Requires at least two independent markers so an ordinary folder that merely
    happens to contain ``full.md`` is not misclassified. The real vault nests
    these as ``Paper/Paper/auto/``, so this must hold regardless of what the
    parent directory contains.

    A genuine paper folder essentially never carries two of
    ``_middle.json`` / ``_model.json`` / ``_layout.pdf`` / ``_span.pdf``.
    """
    if not path.is_dir():
        return False
    try:
        names = [entry.name for entry in path.iterdir() if entry.is_file()]
    except OSError:
        return False
    hits = 0
    for name in names:
        if name == "full.md":
            hits += 1
            continue
        for marker in _MINERU_MARKERS:
            if name.endswith(marker):
                hits += 1
                break
    return hits >= 2


def _has_reader_pdf(names: Iterable[str]) -> bool:
    """True when at least one PDF is a real reader copy, not a parse artefact."""
    return any(
        n.lower().endswith(".pdf") and not _LAYOUT_RE.search(n) for n in names
    )


def _nfc_key(rel_path: str) -> str:
    return unicodedata.normalize("NFC", rel_path)


def _sha256_file(path: Path) -> Optional[str]:
    """Content hash of one file, or None if it cannot be read.

    Scanning used to record no hash at all, so version pinning had nothing to
    compare against and a replaced PDF was indistinguishable from an unchanged
    one. Only paper-sized files are hashed here (they already are), so this
    stays cheap relative to the scan itself.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _dir_depth(base: Path, current: Path) -> int:
    try:
        return len(current.relative_to(base).parts)
    except ValueError:
        return 0


def _build_candidate_sources(
    paper_id: str,
    reader_pdfs: Sequence[Path],
    real_md: Dict[str, SourceRole],
    current: Path,
) -> List[PaperSource]:
    """Collect candidate sources for an ambiguous paper.

    Candidates are tagged with `binding_origin=BindingOrigin.DISCOVERY` and
    `is_candidate=True` so readers, annotations and workspace state do not treat
    them as confirmed bindings before human resolution (ADR-006).
    """
    candidates: List[PaperSource] = []
    for pdf in reader_pdfs:
        source = PaperSource(
            source_id=new_source_id(),
            paper_id=paper_id,
            role=SourceRole.ORIGINAL_PDF,
            rel_path=pdf.name,
            rel_path_key_nfc=_nfc_key(pdf.name),
            is_primary=False,
            binding_origin=BindingOrigin.DISCOVERY,
            is_candidate=True,
            binding_confidence=0.5,
            mime_type="application/pdf",
        )
        try:
            stat = pdf.stat()
            source.size_bytes = stat.st_size
            source.mtime_ns = stat.st_mtime_ns
            source.sha256 = _sha256_file(pdf)
        except OSError:
            pass
        candidates.append(source)

    for name, role in sorted(real_md.items()):
        source = PaperSource(
            source_id=new_source_id(),
            paper_id=paper_id,
            role=role,
            rel_path=name,
            rel_path_key_nfc=_nfc_key(name),
            is_primary=False,
            binding_origin=BindingOrigin.DISCOVERY,
            is_candidate=True,
            binding_confidence=0.5,
            mime_type="text/markdown",
        )
        try:
            stat = (current / name).stat()
            source.size_bytes = stat.st_size
            source.mtime_ns = stat.st_mtime_ns
            source.sha256 = _sha256_file(current / name)
        except OSError:
            pass
        candidates.append(source)
    return candidates


def _pick_primary_pdf(
    pdf_names: List[str], folder_name: str
) -> Tuple[Optional[str], List[str]]:
    """Choose the main PDF, or report why the choice is ambiguous.

    Returns ``(chosen, ambiguous_candidates)``. ``_layout.pdf`` / ``_origin.pdf``
    / ``_span.pdf`` are parse artefacts and are excluded before scoring.
    """
    if not pdf_names:
        return None, []

    # Never the main reader: these are parse artefacts.
    usable = [n for n in pdf_names if not _LAYOUT_RE.search(n)]
    if not usable:
        return None, []
    if len(usable) == 1:
        return usable[0], []

    folder_key = normalize_title(folder_name)
    exact = [n for n in usable if normalize_title(n[: -len(_PDF_SUFFIX)]) == folder_key]
    if len(exact) == 1:
        return exact[0], []

    return None, usable


def _looks_like_pdf(path: Path) -> bool:
    """Cheap magic-byte check. A ``.pdf`` extension alone is not proof."""
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def is_valid_utf8_text(path: Path, sniff_bytes: int = 4096) -> bool:
    """True when the file looks like readable UTF-8 text.

    Decoding a fixed-size prefix used to reject perfectly valid Chinese files:
    UTF-8 encodes most CJK characters in 3 bytes, so an arbitrary cut can land in
    the middle of a character and raise ``UnicodeDecodeError`` on a file that is
    entirely well-formed. (Measured on the real vault: a translation was flagged
    invalid and its paper silently downgraded to DEGRADED.)

    So only the *complete* characters inside the sniff window are validated: a
    trailing partial sequence is expected at the boundary and is not an error.
    A file that is genuinely not UTF-8 still fails, because the invalid sequence
    then appears somewhere before the cut.
    """
    try:
        with path.open("rb") as handle:
            chunk = handle.read(sniff_bytes)
    except OSError:
        return False

    if not chunk:
        return True

    # Drop at most 3 trailing bytes (UTF-8 sequences are at most 4 bytes, so a
    # partial character straddling the cut can be no longer than that).
    for trim in range(0, 4):
        candidate = chunk[: len(chunk) - trim] if trim else chunk
        try:
            candidate.decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


def discover_papers(config: ScanConfig) -> ScanResult:
    """Walk ``config.root`` and return discovered papers.

    Read-only with respect to the Vault: no file is created, modified or moved.
    """
    result = ScanResult()
    root = config.root.expanduser()
    if not root.is_dir():
        result.errors.append(f"scan root not found: {root}")
        return result

    exclude = set(config.exclude_dirs)
    for current in sorted(root.rglob("*")):
        if not current.is_dir() or current.is_symlink():
            continue
        if current.name in exclude:
            continue
        if _dir_depth(root, current) > config.max_depth:
            continue

        try:
            entries = [p for p in current.iterdir() if not p.is_symlink()]
        except OSError as exc:
            result.errors.append(f"cannot list {current}: {exc}")
            continue

        # A MinerU container is an artefact, never a paper, regardless of what
        # the parent directory holds. The real vault nests them one level
        # deeper than the paper folder (Paper/Paper/auto/).
        if config.detect_mineru and is_mineru_artifact_dir(current):
            result.mineru_containers.append(current.relative_to(root).as_posix())
            continue

        folder_relpath = current.relative_to(root).as_posix()
        manifest_file = current / MANIFEST_FILENAME
        manifest_exists = False
        try:
            st_mf = os.lstat(manifest_file)
            if stat_mod.S_ISLNK(st_mf.st_mode) or not stat_mod.S_ISREG(st_mf.st_mode):
                # P0-C: Manifest file itself is a symlink or special file -> FAIL CLOSED!
                result.errors.append(
                    f"manifest file {manifest_file} is a symlink or special file in {folder_relpath}"
                )
                continue
            manifest_exists = True
        except FileNotFoundError:
            manifest_exists = False
        except OSError as exc:
            result.errors.append(f"cannot lstat manifest in {folder_relpath}: {exc}")
            continue

        if manifest_exists:
            try:
                raw = manifest_file.read_bytes()
                manifest_doc = json.loads(raw.decode("utf-8"))
                if not isinstance(manifest_doc, dict):
                    raise ValueError("manifest is not a JSON object")
                parsed_manifest = parse_manifest(manifest_doc)
            except Exception as exc:
                # Corrupt or unreadable manifest: FAIL CLOSED.
                # Must not silently create a new identity or treat as ambiguous.
                result.errors.append(f"corrupt manifest in {folder_relpath}: {exc}")
                continue

            manifest_sources: List[PaperSource] = []
            missing_active = False
            semantic_invalid = False
            primary_pdf = None
            primary_tr = None

            for s_entry in parsed_manifest.sources:
                rel_path = s_entry["path"]
                try:
                    role = SourceRole(s_entry["role"])
                except ValueError:
                    role = SourceRole.OTHER_MARKDOWN
                    semantic_invalid = True
                is_pri = bool(s_entry.get("primary", False))
                act = bool(s_entry.get("active", True))

                source = PaperSource(
                    source_id=s_entry["source_id"],
                    paper_id=parsed_manifest.paper_id,
                    role=role,
                    rel_path=rel_path,
                    rel_path_key_nfc=_nfc_key(rel_path),
                    is_primary=is_pri,
                    binding_origin=BindingOrigin.MANIFEST,
                    binding_confidence=1.0,
                    active=act,
                )

                # Check lexical path for symlinks (P0-4: symlink ancestor check)
                lexical = current
                has_symlink = False
                for part in Path(rel_path).parts:
                    lexical = lexical / part
                    try:
                        st_lex = os.lstat(lexical)
                        if stat_mod.S_ISLNK(st_lex.st_mode):
                            has_symlink = True
                            break
                    except FileNotFoundError:
                        break

                target_file = lexical
                file_exists = target_file.is_file() and not has_symlink and not os.path.islink(target_file)

                if has_symlink:
                    result.errors.append(f"manifest source {rel_path} contains symlink in {folder_relpath}")
                    semantic_invalid = True

                if file_exists:
                    try:
                        st = target_file.stat()
                        source.size_bytes = st.st_size
                        source.mtime_ns = st.st_mtime_ns
                        source.sha256 = _sha256_file(target_file)
                    except OSError:
                        pass

                    # P0-3: Semantic role and media validation on physical file
                    if act:
                        if role in (SourceRole.ORIGINAL_PDF, SourceRole.SUPPLEMENTAL_PDF):
                            if not rel_path.lower().endswith(_PDF_SUFFIX) or not _looks_like_pdf(target_file):
                                semantic_invalid = True
                        else:
                            if not rel_path.lower().endswith(_MD_SUFFIX):
                                semantic_invalid = True
                            elif not is_valid_utf8_text(target_file):
                                semantic_invalid = True
                else:
                    if act:
                        missing_active = True
                        source.missing_since = utc_now()
                        source.active = False

                if is_pri and role in (SourceRole.ORIGINAL_PDF, SourceRole.SUPPLEMENTAL_PDF) and source.active:
                    primary_pdf = source
                if is_pri and role.is_translation and source.active:
                    primary_tr = source

                manifest_sources.append(source)

            # Direct files on disk not declared in manifest: candidate / potential rename sources (ADR-006)
            manifest_paths = {s.rel_path for s in manifest_sources}
            direct_files = [e for e in entries if e.is_file() and not e.is_symlink()]
            for f in direct_files:
                if f.name == MANIFEST_FILENAME or f.name in manifest_paths:
                    continue
                if _LAYOUT_RE.search(f.name):
                    continue
                f_role = (
                    SourceRole.SUPPLEMENTAL_PDF
                    if f.name.lower().endswith(_PDF_SUFFIX)
                    else classify_markdown_role(f.name)
                )
                if f_role is not None:
                    source = PaperSource(
                        source_id=new_source_id(),
                        paper_id=parsed_manifest.paper_id,
                        role=f_role,
                        rel_path=f.name,
                        rel_path_key_nfc=_nfc_key(f.name),
                        is_primary=False,
                        binding_origin=BindingOrigin.DISCOVERY,
                        is_candidate=True,
                        active=False,
                    )
                    try:
                        st = f.stat()
                        source.size_bytes = st.st_size
                        source.mtime_ns = st.st_mtime_ns
                        source.sha256 = _sha256_file(f)
                    except OSError:
                        pass
                    manifest_sources.append(source)

            # Semantic validity check:
            # - P0-1: Bound sources ONLY come from manifest. Candidates do not participate in health!
            # - P0-2: No heuristic primary elevation. primary is strictly what manifest declared.
            manifest_only = [s for s in manifest_sources if not s.is_candidate]
            has_active = any(s.active for s in manifest_only)
            primary_pdf_count = len(
                [s for s in manifest_only if s.is_primary and s.media_kind is MediaKind.PDF and s.active]
            )
            primary_tr_count = len(
                [s for s in manifest_only if s.is_primary and s.role.is_translation and s.active]
            )
            semantic_valid = (
                has_active
                and not semantic_invalid
                and primary_pdf_count <= 1
                and primary_tr_count <= 1
            )

            if not semantic_valid or missing_active:
                binding_state = BindingState.DEGRADED
            else:
                binding_state = BindingState.ADOPTED

            paper = Paper(
                paper_id=parsed_manifest.paper_id,
                folder_relpath=folder_relpath,
                display_title=current.name,
                title_override=manifest_doc.get("title_override"),
                category_relpath="",
                manifest_relpath=MANIFEST_FILENAME,
                binding_state=binding_state,
                note_id=parsed_manifest.note_id,
                paper_tags=list(manifest_doc.get("tags") or []),
                external_ids=dict(manifest_doc.get("external_ids") or {}),
                primary_pdf_source_id=primary_pdf.source_id if primary_pdf else None,
                primary_translation_source_id=primary_tr.source_id if primary_tr else None,
                sources=manifest_sources,
            )
            result.papers.append(paper)
            continue

        direct_files = [e for e in entries if e.is_file()]

        pdf_files = [e for e in direct_files if e.name.lower().endswith(_PDF_SUFFIX)]
        md_files = [e for e in direct_files if e.name.lower().endswith(_MD_SUFFIX)]

        # Reader PDFs only: _layout/_origin/_span are parse artefacts and are
        # never bound as paper sources.
        reader_pdfs = [p for p in pdf_files if not _LAYOUT_RE.search(p.name)]
        artefact_pdfs = [p for p in pdf_files if _LAYOUT_RE.search(p.name)]
        _ = artefact_pdfs  # recorded implicitly via mineru_containers

        has_pdf = any(_looks_like_pdf(p) for p in reader_pdfs) if reader_pdfs else False
        md_roles = {
            md.name: classify_markdown_role(md.name)
            for md in md_files
        }
        real_md = {name: role for name, role in md_roles.items() if role is not None}

        if not has_pdf and not real_md:
            # Not a paper folder. Recursion is handled by rglob.
            continue

        paper = Paper(
            paper_id="",  # assigned only at adoption; discovery is identity-free
            folder_relpath=folder_relpath,
            display_title=current.name,
            category_relpath="",
            binding_state=BindingState.DISCOVERED,
        )

        if not has_pdf:
            # Only markdown, no PDF: do not auto-adopt (ADR-006). Report it.
            paper.binding_state = BindingState.AMBIGUOUS
            paper.ambiguity_reason = "NO_PDF_MARKDOWN_ONLY"
            paper.sources = _build_candidate_sources(
                paper.paper_id, reader_pdfs, real_md, current
            )
            result.ambiguous.append(paper)
            continue

        pdf_names = [p.name for p in reader_pdfs]
        chosen, ambiguous_pdfs = _pick_primary_pdf(pdf_names, current.name)

        if ambiguous_pdfs:
            paper.binding_state = BindingState.AMBIGUOUS
            paper.ambiguity_reason = "MULTIPLE_PDFS"
            paper.sources = _build_candidate_sources(
                paper.paper_id, reader_pdfs, real_md, current
            )
            result.ambiguous.append(paper)
            continue

        sources: List[PaperSource] = []
        for pdf in reader_pdfs:
            role = (
                SourceRole.ORIGINAL_PDF
                if chosen and pdf.name == chosen
                else SourceRole.SUPPLEMENTAL_PDF
            )
            source = PaperSource(
                source_id=new_source_id(),
                paper_id=paper.paper_id,
                role=role,
                rel_path=pdf.name,
                rel_path_key_nfc=_nfc_key(pdf.name),
                is_primary=(pdf.name == chosen),
                binding_origin=BindingOrigin.STRICT_RULE,
                binding_confidence=1.0 if pdf.name == chosen else 0.5,
                mime_type="application/pdf",
            )
            try:
                stat = pdf.stat()
                source.size_bytes = stat.st_size
                source.mtime_ns = stat.st_mtime_ns
                source.sha256 = _sha256_file(pdf)
            except OSError:
                pass
            sources.append(source)

        for name, role in sorted(real_md.items()):
            source = PaperSource(
                source_id=new_source_id(),
                paper_id=paper.paper_id,
                role=role,
                rel_path=name,
                rel_path_key_nfc=_nfc_key(name),
                is_primary=(role is SourceRole.TRANSLATION_FULL),
                binding_origin=(
                    BindingOrigin.STRICT_RULE
                    if role is not SourceRole.OTHER_MARKDOWN
                    else BindingOrigin.MANUAL
                ),
                binding_confidence=(
                    1.0 if role is not SourceRole.OTHER_MARKDOWN else 0.3
                ),
                mime_type="text/markdown",
            )
            try:
                stat = (current / name).stat()
                source.size_bytes = stat.st_size
                source.mtime_ns = stat.st_mtime_ns
                source.sha256 = _sha256_file(current / name)
            except OSError:
                pass
            sources.append(source)

        if not any(s.role.is_translation for s in sources):
            paper.binding_state = BindingState.PDF_ONLY
        else:
            paper.binding_state = BindingState.RESOLVED

        primary_pdf = next((s for s in sources if s.is_primary and s.media_kind is MediaKind.PDF), None)
        # Guarantee exactly one primary PDF even when only supplemental remained.
        if primary_pdf is None:
            for source in sources:
                if source.media_kind is MediaKind.PDF:
                    source.is_primary = True
                    source.role = SourceRole.ORIGINAL_PDF
                    primary_pdf = source
                    break
        if primary_pdf is not None:
            paper.primary_pdf_source_id = primary_pdf.source_id

        primary_translation = next(
            (s for s in sorted(sources, key=lambda s: s.role.display_rank) if s.role.is_translation),
            None,
        )
        if primary_translation is not None:
            paper.primary_translation_source_id = primary_translation.source_id
            for source in sources:
                if source.role.is_translation:
                    source.is_primary = source is primary_translation

        paper.sources = sources
        paper.created_at = utc_now()
        paper.updated_at = utc_now()
        result.papers.append(paper)

    return result
