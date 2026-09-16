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
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from ..state import get_cfg
from . import storage as paper_storage
from .models import MediaKind, PaperSource
from .writer import VaultWriteService

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


def _iter_file_range(path: Path, start: int, end: int):
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _get_source_or_404(source_id: str) -> Tuple[PaperSource, Path]:
    storage = paper_storage.PaperStorage()
    source = storage.get_source(source_id)
    if source is None or not source.active:
        raise HTTPException(404, f"unknown paper source: {source_id}")
    if source.media_kind is not MediaKind.PDF:
        raise HTTPException(415, "source is not a PDF")

    paper = storage.get_paper(source.paper_id)
    if paper is None:
        raise HTTPException(404, f"unknown paper for source: {source_id}")

    cfg = get_cfg()
    # folder_relpath 相对于 papers 扫描根，而非 Vault 根（两者可不同）。
    # 安全边界仍必须以 Vault 为上限，且 papers 根本身必须落在 Vault 内。
    service = VaultWriteService(cfg.vault_root)
    papers_root = cfg.papers_root_or_default
    try:
        papers_root.relative_to(cfg.vault_root)
    except ValueError as exc:
        raise HTTPException(500, "papers root escapes the vault; refusing to serve") from exc

    full = papers_root / paper.folder_relpath / source.rel_path
    resolved = full.resolve(strict=False)
    try:
        resolved.relative_to(cfg.vault_root)
    except ValueError as exc:
        raise HTTPException(400, "resolved source path escapes the vault") from exc

    if not resolved.is_file():
        raise HTTPException(404, f"source file is missing on disk: {source.rel_path}")
    return source, resolved


@router.get("/{source_id}/content")
def get_source_content(
    source_id: str,
    request: Request,
    version: Optional[int] = None,
    range_header: Optional[str] = Header(None, alias="Range"),
):
    source, full = _get_source_or_404(source_id)

    # Version pinning: if the caller pinned a version and the binding moved on,
    # refuse rather than serving bytes from a different revision of the file.
    if version is not None and version != source.source_version:
        raise HTTPException(
            412, f"source version changed: requested {version}, current {source.source_version}"
        )

    stat = full.stat()
    size = stat.st_size
    etag = f'"{source.source_version}-{int(stat.st_mtime_ns)}-{size}"'

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
                _iter_file_range(full, start, end),
                status_code=206,
                media_type="application/pdf",
                headers=headers,
            )

    headers["Content-Length"] = str(size)
    return FileResponse(
        full,
        media_type="application/pdf",
        headers=headers,
    )


@router.head("/{source_id}/content")
def head_source_content(
    source_id: str,
    version: Optional[int] = None,
):
    """HEAD must expose exactly the same headers, with no body."""
    source, full = _get_source_or_404(source_id)
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
