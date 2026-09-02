"""Markdown parser: frontmatter, tags (分类), 状态 (生命周期), metadata typing, sha256."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import frontmatter

from ..database.sqlite import classify_value

STATUS_KEYS = ("状态", "status")
# tags / 状态 职责边界（P0-MUST-1）：状态 只进 metadata，永不进 tags 表
TAG_KEYS = ("tags", "标签")


@dataclass
class ParsedFile:
    path: Path
    rel_path: str
    filename: str
    title: str
    folder: str
    size: int
    created_at: str | None
    modified_at: str | None
    sha256: str
    tags: list[str] = field(default_factory=list)      # 分类（tags/标签）
    statuses: list[str] = field(default_factory=list)   # 生命周期（状态/status）
    metadata: dict[str, tuple[str, str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "path": self.rel_path,
            "filename": self.filename,
            "title": self.title,
            "folder": self.folder,
            "size": self.size,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "hash": self.sha256,
            "tags": self.tags,
            "statuses": self.statuses,
            "metadata": {k: v[0] for k, v in self.metadata.items()},
        }


def _norm_ts(ts) -> str | None:
    if isinstance(ts, datetime):
        return ts.isoformat()
    if ts is None:
        return None
    s = str(ts).strip()
    return s or None


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.replace(",", " ").split() if v.strip()]
    return [str(value)]


def parse_markdown(path: Path, vault_root: Path, raw_bytes: bytes | None = None) -> ParsedFile:
    """Parse one markdown file. Caller guarantees exclusions were applied.
    P1-M2-2: raw_bytes 由调用方一次性读取（确保 raw 与 hash 来自同一 snapshot）。"""
    rel = path.relative_to(vault_root).as_posix()
    st = path.stat()
    if raw_bytes is None:
        raw_bytes = path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8", errors="replace")

    post = frontmatter.loads(text)
    fm = post.metadata or {}

    tags: list[str] = []
    statuses: list[str] = []
    metadata: dict[str, tuple[str, str]] = {}

    for key, value in fm.items():
        vtype, vtext = classify_value(value)
        metadata[str(key)] = (vtext, vtype)
        if str(key) in TAG_KEYS:
            tags.extend(_as_list(value))
        elif str(key) in STATUS_KEYS:
            statuses.extend(_as_list(value))

    # tags 去重保序；状态 独立
    seen: set[str] = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]

    title = str(fm.get("title") or "").strip() or path.stem
    return ParsedFile(
        path=path,
        rel_path=rel,
        filename=path.name,
        title=title,
        folder=path.parent.relative_to(vault_root).as_posix(),
        size=st.st_size,
        created_at=_norm_ts(fm.get("创建时间") or fm.get("created")),
        modified_at=datetime.fromtimestamp(st.st_mtime).isoformat(),
        sha256=sha256,
        tags=tags,
        statuses=statuses,
        metadata=metadata,
    )
