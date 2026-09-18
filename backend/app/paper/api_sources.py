"""PDF source content endpoint.

``path_guard`` deliberately excludes PDF from the generic asset allowlist,
because that allowlist exists to stop arbitrary binaries being served from a
path-shaped URL. A PDF reader needs the opposite: a dedicated, single-purpose,
range-capable, read-only endpoint keyed by an opaque ``source_id``.

Why an opaque ID rather than a path: the real vault is full of Chinese names,
emoji, spaces and full-width colons, and macOS stores them as NFD while other
tools produce NFC. A path-keyed API has to survive double URL-decoding and
normalisation mismatches. ``source_id`` sidesteps all of it (ADR-009).

The endpoint implements the full byte-range contract because a 23 MB PDF cannot
be rendered page-by-page otherwise:

* ``200`` full body, ``206`` single range with ``Content-Range``, ``416`` for an
  unsatisfiable range;
* ``HEAD`` returning the same headers with no body;
* ``Accept-Ranges: bytes`` and an accurate ``Content-Length``;
* RFC 5987 ``filename*`` so Chinese filenames survive;
* ``X-Content-Type-Options: nosniff`` and ``CORP: same-origin``;
* an ``ETag``/version check so a PDF replaced mid-session cannot lead to PDF.js
  stitching together bytes from two different files.
"""

from __future__ import annotations

import mimetypes
import os
import re
import stat
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..state import get_cfg
from . import storage as paper_storage
from .models import MediaKind, PaperSource

router = APIRouter(prefix="/api/paper-sources", tags=["paper-sources"])

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)

#: Chunk size for streamed range responses.
_CHUNK = 1024 * 256


def _strip_range_header(request: Request) -> None:
    """Remove the Range header from the ASGI scope.

    Starlette's ``FileResponse`` applies its own range handling whenever it sees
    a Range header. If this module answers ranges itself but leaves the header
    in place, a request this module decides to ignore (multi-range or malformed)
    would still be silently range-processed downstream — two different range
    behaviours from one endpoint. Owning the decision means owning the header.
    """
    scope = request.scope
    headers = scope.get("headers")
    if headers:
        scope["headers"] = [
            (name, value) for name, value in headers if name.lower() != b"range"
        ]


def _apply_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"


def _content_disposition(filename: str, inline: bool = True) -> str:
    """RFC 5987 disposition so non-ASCII filenames survive."""
    disposition = "inline" if inline else "attachment"
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "document.pdf"
    # Quote any character that would break the quoted-string form.
    ascii_fallback = re.sub(r'[\\"\r\n]', "_", ascii_fallback)
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """Parse a single byte range. Returns inclusive (start, end) or None.

    Raises ``ValueError`` for a syntactically valid but unsatisfiable range so
    the caller can answer 416, as the spec requires.
    """
    raw_header = header.strip()
    if "," in raw_header:
        return None  # multi-range: ignore, serve the whole entity
    match = _RANGE_RE.match(raw_header)
    if match is None:
        return None  # malformed -> ignore, serve the whole entity
    raw_start, raw_end = match.group(1), match.group(2)

    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":
        # Suffix form: last N bytes.
        length = int(raw_end)
        if length == 0:
            raise ValueError("zero-length suffix range")
        start = max(0, size - length)
        return start, size - 1

    start = int(raw_start)
    if start >= size:
        raise ValueError("range start beyond end of file")

    if raw_end == "":
        return start, size - 1
    end = min(int(raw_end), size - 1)
    if end < start:
        raise ValueError("range end before start")
    return start, end


def _iter_fd_range(fd: int, start: int, end: int):
    """Stream a byte range from an already-open descriptor.

    The descriptor is opened once, before the response begins, and both the
    size and the version check come from that same handle. Opening the path
    inside the generator would leave a window between stat() and the first read
    during which the file could be replaced, letting one response mix bytes from
    two revisions — exactly what the version pin exists to prevent.
    """
    remaining = end - start + 1
    os.lseek(fd, start, os.SEEK_SET)
    while remaining > 0:
        chunk = os.read(fd, min(_CHUNK, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk


def _open_pinned(path: Path) -> Tuple[int, os.stat_result]:
    """Open a file and return (fd, fstat) for the same underlying object."""
    fd = os.open(path, os.O_RDONLY)
    try:
        stat = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.st_size and stat.st_size != 0:
        os.close(fd)
        raise HTTPException(500, "source has an invalid size")
    return fd, stat


def _get_source_or_404(source_id: str) -> Tuple[PaperSource, Path]:
    storage = paper_storage.PaperStorage()
    source = storage.get_source(source_id)
    if source is None or not source.active:
        raise HTTPException(404, f"unknown paper source: {source_id}")
    if source.media_kind not in (MediaKind.PDF, MediaKind.MARKDOWN):
        raise HTTPException(415, "source is not readable")

    paper = storage.get_paper(source.paper_id)
    if paper is None:
        raise HTTPException(404, f"unknown paper for source: {source_id}")

    cfg = get_cfg()
    # folder_relpath 相对于 papers 扫描根，而非 Vault 根（两者可不同）。
    # 安全边界仍必须以 Vault 为上限，且 papers 根本身必须落在 Vault 内。
    papers_root = cfg.papers_root_or_default
    try:
        papers_root.relative_to(cfg.vault_root)
    except ValueError as exc:
        raise HTTPException(500, "papers root escapes the vault; refusing to serve") from exc

    paper_dir = (papers_root / paper.folder_relpath).resolve()
    lexical = paper_dir
    for part in Path(source.rel_path).parts:
        lexical = lexical / part
        try:
            st = os.lstat(lexical)
            if stat.S_ISLNK(st.st_mode):
                raise HTTPException(400, f"symlink forbidden: {source.rel_path}")
        except FileNotFoundError:
            raise HTTPException(404, f"source file is missing on disk: {source.rel_path}")

    if not lexical.is_file() or os.path.islink(lexical):
        raise HTTPException(404, f"source file is missing or is a symlink: {source.rel_path}")

    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(cfg.vault_root)
    except ValueError as exc:
        raise HTTPException(400, "resolved source path escapes the vault") from exc

    return source, resolved


@router.get("/{source_id}/text")
def get_source_text(source_id: str):
    """Raw Markdown for a translation or extraction source.

    Returned as text/plain so it is never interpreted as active markup by the
    browser. The frontend renders it through the host page's sanitising
    pipeline, which is the single owner of that decision.
    """
    source, full = _get_source_or_404(source_id)
    if source.media_kind is not MediaKind.MARKDOWN:
        raise HTTPException(415, "source is not a Markdown document")
    if full.stat().st_size > 8 * 1024 * 1024:
        raise HTTPException(413, "markdown source is too large to render")
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(500, f"cannot read source: {exc}") from exc
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{source_id}/content")
def get_source_content(
    source_id: str,
    request: Request,
    version: Optional[int] = None,
    range_header: Optional[str] = Header(None, alias="Range"),
):
    source, full = _get_source_or_404(source_id)
    # Byte-range semantics are PDF-specific. Markdown is served by /text, so
    # refusing here keeps one code path per media kind.
    if source.media_kind is not MediaKind.PDF:
        raise HTTPException(415, "use /text for Markdown sources")

    # Version pinning: if the caller pinned a version and the binding moved on,
    # refuse rather than serving bytes from a different revision of the file.
    if version is not None and version != source.source_version:
        raise HTTPException(
            412, f"source version changed: requested {version}, current {source.source_version}"
        )

    # Pin the handle first: the size, the ETag and the bytes all come from the
    # same open file, so a replacement mid-response cannot mix revisions.
    fd, file_stat = _open_pinned(full)
    size = file_stat.st_size
    etag = f'"{source.source_version}-{int(file_stat.st_mtime_ns)}-{size}"'

    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Content-Disposition": _content_disposition(full.name),
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }

    if range_header:
        # This endpoint owns range handling; stop FileResponse from applying a
        # second, different range policy behind our back.
        _strip_range_header(request)
        try:
            parsed = _parse_range(range_header, size)
        except ValueError:
            os.close(fd)
            headers["Content-Range"] = f"bytes */{size}"
            raise HTTPException(416, "requested range not satisfiable", headers=headers)
        if parsed is not None:
            start, end = parsed
            length = end - start + 1
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(length)
            # Compression middleware must not touch this: gzip would destroy
            # the byte-range contract.
            headers["Content-Encoding"] = "identity"
            return StreamingResponse(
                _stream_and_close(fd, start, end),
                status_code=206,
                media_type="application/pdf",
                headers=headers,
            )

    # No range: stream the same pinned descriptor and close it when done, so no
    # code path leaves a descriptor behind.
    headers["Content-Length"] = str(size)
    return StreamingResponse(
        _stream_and_close(fd, 0, size - 1),
        status_code=200,
        media_type="application/pdf",
        headers=headers,
    )


def _stream_and_close(fd: int, start: int, end: int):
    """Stream a range then close the descriptor, including on client abort."""
    try:
        yield from _iter_fd_range(fd, start, end)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@router.head("/{source_id}/content")
def head_source_content(
    source_id: str,
    version: Optional[int] = None,
):
    """HEAD must expose exactly the same headers, with no body."""
    source, full = _get_source_or_404(source_id)
    if source.media_kind is not MediaKind.PDF:
        raise HTTPException(415, "use /text for Markdown sources")
    if version is not None and version != source.source_version:
        raise HTTPException(412, f"source version changed: requested {version}")
    stat = full.stat()
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(stat.st_size),
        "Content-Type": "application/pdf",
        "ETag": f'"{source.source_version}-{int(stat.st_mtime_ns)}-{stat.st_size}"',
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Content-Disposition": _content_disposition(full.name),
        "Cache-Control": "no-store, no-cache, must-revalidate",
    }
    return Response(status_code=200, headers=headers)


# ---------------------------------------------------------------------------
# Source-relative assets
# ---------------------------------------------------------------------------


def _resolve_source_relative(markdown_path: Path, asset_ref: str) -> Path:
    """Resolve an asset referenced by a paper's Markdown, downward only.

    MinerU extractions routinely reference images in a sibling ``images/``
    folder, and the generic vault asset endpoint answers such a reference by
    scanning the entire vault for a matching basename. That is both slow and
    ambiguous — two papers shipping an ``image_1.png`` would collide, and the
    resolver could pick the wrong paper's figure.

    Resolution is therefore restricted to the referring document's own folder
    and its descendants. Nothing above the paper folder is ever consulted, so a
    reference can never escape into another paper.
    """
    if not asset_ref or "\x00" in asset_ref:
        raise HTTPException(400, "invalid asset reference")
    if asset_ref.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", asset_ref):
        raise HTTPException(400, "absolute asset paths are not permitted")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", asset_ref):
        # scheme-qualified (http:, data:, ...) never reaches here; a local
        # reference must stay local.
        raise HTTPException(400, "external asset references are not permitted")

    parts = Path(asset_ref).parts
    if any(part == ".." for part in parts):
        raise HTTPException(400, "asset reference may not traverse upwards")

    base = markdown_path.parent
    # Lexical symlink check
    lexical = base
    for part in parts:
        lexical = lexical / part
        try:
            st = os.lstat(lexical)
            if stat.S_ISLNK(st.st_mode):
                raise HTTPException(400, "symlinked assets are not permitted")
        except FileNotFoundError:
            break

    candidate = (base / asset_ref).resolve(strict=False)

    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(400, "asset reference escapes the paper folder") from exc

    if candidate.is_symlink():
        raise HTTPException(400, "symlinked assets are not permitted")
    if not candidate.is_file():
        # Some extractions place images one level down under images/.
        for sub in ("images", "assets", "figures"):
            alt = (base / sub / asset_ref).resolve(strict=False)
            try:
                alt.relative_to(base)
            except ValueError:
                continue
            if alt.is_file() and not alt.is_symlink():
                return alt
        raise HTTPException(404, f"asset not found: {asset_ref}")
    return candidate


@router.get("/{source_id}/asset")
def get_source_asset(
    source_id: str,
    ref: str,
    version: Optional[int] = None,
):
    """Serve an image referenced by a paper's Markdown, same-origin only.

    Returning bytes from our own origin is what lets the reader display figures
    without contacting anything external, which is the zero-egress promise.
    """
    source, full = _get_source_or_404(source_id)
    if source.media_kind is not MediaKind.MARKDOWN:
        raise HTTPException(415, "assets are resolved relative to a Markdown source")
    if version is not None and version != source.source_version:
        raise HTTPException(412, f"source version changed: requested {version}")

    resolved = _resolve_source_relative(full, ref)
    media_type, _ = mimetypes.guess_type(str(resolved))
    if media_type not in _ALLOWED_ASSET_MEDIA:
        raise HTTPException(
            415, f"asset type is not served inline: {media_type or 'unknown'}"
        )

    with resolved.open("rb") as handle:
        payload = handle.read()
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )


#: Passive raster and font formats only. SVG is an active format (it can carry
#: script and external references) and PDFs are handled by their own endpoint.
_ALLOWED_ASSET_MEDIA = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    }
)
