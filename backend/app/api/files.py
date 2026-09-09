"""API: config / files (tree, content, save, create, status).
P1-M2-2: 单次字节快照、per-path 锁、原子 replace、O_EXCL 创建。
P1-M2-3: status 语义统一。P1-M2-4: operation-aware 路径边界。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
import mimetypes
import unicodedata
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..database import sqlite
from ..deps import get_conn
from ..scanner.parser import parse_markdown
from ..scanner.vault_scanner import upsert_one
from ..security.path_guard import (
    PathError,
    resolve_for_asset_read,
    resolve_for_create,
    resolve_for_read_snapshot,
    resolve_for_write,
)
from ..state import get_cfg
from ..status import pick_status

router = APIRouter()

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    # Sol P2: key 改为 canonical/NFC resolved path，避免相对/绝对路径别名竞争
    try:
        cfg = get_cfg()
        full = (cfg.vault_root / path).resolve(strict=False)
        key = unicodedata.normalize("NFC", str(full))
    except Exception:
        key = unicodedata.normalize("NFC", path)
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


# ---------- models ----------

class SaveRequest(BaseModel):
    path: str
    content: str
    expected_hash: str


class CreateRequest(BaseModel):
    path: str
    content: str


class StatusRequest(BaseModel):
    path: str
    status: str
    expected_hash: str


# ---------- helpers ----------

# Sol P2: 常量 SQL（无外部输入），批量拉取 tags/metadata 消除每文件 N+1
_SQL_ALL_TAGS = (
    "SELECT ft.file_id AS file_id, t.name AS name "
    "FROM file_tags ft JOIN tags t ON t.id = ft.tag_id ORDER BY t.name"
)
_SQL_ALL_META = "SELECT file_id, key, value, value_type FROM metadata"


def _file_payloads_bulk(conn) -> list[dict]:
    files = conn.execute("SELECT * FROM files ORDER BY path").fetchall()
    tags_by_file: dict[int, list[str]] = {}
    for r in conn.execute(_SQL_ALL_TAGS):
        tags_by_file.setdefault(r["file_id"], []).append(r["name"])
    meta_by_file: dict[int, list] = {}
    for r in conn.execute(_SQL_ALL_META):
        meta_by_file.setdefault(r["file_id"], []).append(r)

    payloads = []
    for row in files:
        status = pick_status(meta_by_file.get(row["id"], []))
        payloads.append(
            {
                "path": row["path"],
                "filename": row["filename"],
                "title": row["title"],
                "folder": row["folder"],
                "size": row["size"],
                "modified_at": row["modified_at"],
                "hash": row["hash"],
                "tags": tags_by_file.get(row["id"], []),
                "statuses": status[1] if status else [],
            }
        )
    return payloads


def _check_conflict(full: Path, expected_hash: str) -> None:
    current = hashlib.sha256(full.read_bytes()).hexdigest()
    if current != expected_hash:
        raise HTTPException(409, "文件已被外部修改，请重新加载")


def _backup(full: Path) -> None:
    backup_dir = get_cfg().database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe = full.relative_to(get_cfg().vault_root).as_posix().replace("/", "__")
    target = backup_dir / f"{hashlib.sha256(str(full).encode()).hexdigest()[:8]}_{safe}.bak"
    target.write_bytes(full.read_bytes())


def _atomic_write(full: Path, content: str) -> None:
    """原子替换（P1-M2-2）：同目录 temp + fsync + os.replace。"""
    tmp = full.with_name(f".{full.name}.ws-tmp-{uuid.uuid4().hex}")
    try:
        tmp.resolve(strict=False).relative_to(full.parent.resolve(strict=False))
    except (OSError, ValueError):
        # full 名异常或 symlink 指向 vault 外：temp 绝不写到边界之外
        raise HTTPException(400, "path traversal rejected")
    tmp.write_text(content, encoding="utf-8")
    fd = os.open(tmp, os.O_RDONLY)  # write_text 不落盘：fsync 后再 replace
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, full)


# ---------- endpoints ----------

@router.get("/config")
def get_config(conn=Depends(get_conn)):
    cfg = get_cfg()
    s = sqlite.stats(conn)
    return {
        "vault_path": str(cfg.vault_root),
        "templates_dir": str(cfg.templates_dir),
        "stats": {
            "files": s["files"],
            "tags": s["tags"],
            "last_scan": s["last_scan"],
        },
    }


@router.get("/files/tree")
def files_tree(conn=Depends(get_conn)):
    payloads = _file_payloads_bulk(conn)
    root: dict = {"name": "", "path": "", "type": "dir", "children": []}
    # Sol P2：目录节点建 dict 索引，把每文件的建树开销从 O(深度×兄弟数) 降为 O(深度)
    dirs_by_path: dict[str, dict] = {}
    for f in payloads:
        parts = f["path"].split("/")
        node = root
        for i, part in enumerate(parts):
            dir_path = "/".join(parts[: i + 1])
            if i < len(parts) - 1:
                child = dirs_by_path.get(dir_path)
                if child is None:
                    child = {"name": part, "path": dir_path, "type": "dir", "children": []}
                    dirs_by_path[dir_path] = child
                    node["children"].append(child)
                node = child
            else:
                node["children"].append({**f, "type": "file"})
    return root


@router.get("/file/content")
def file_content(path: str = Query(...), conn=Depends(get_conn)):
    try:
        full, raw_bytes = resolve_for_read_snapshot(get_cfg(), path)  # MUST-2: canonical + 单次快照
    except PathError as e:
        raise HTTPException(400, str(e))
    if not full.is_file():
        raise HTTPException(404, f"file not found: {path}")
    sha = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8", errors="replace")
    parsed = parse_markdown(full, get_cfg().vault_root, raw_bytes=raw_bytes)
    meta = {}
    for k, (v, vt) in parsed.metadata.items():
        meta[k] = json.loads(v) if vt == "list" else v
    return {
        "path": path,
        "raw": text,
        "hash": sha,
        "tags": parsed.tags,
        "statuses": parsed.statuses,
        "metadata": meta,
    }


@router.post("/file/save")
def file_save(req: SaveRequest, conn=Depends(get_conn)):
    with _lock_for(req.path):  # P1-M2-2: per-path 临界区
        try:
            full = resolve_for_write(get_cfg(), req.path)
        except PathError as e:
            raise HTTPException(400, str(e))
        if not full.is_file():
            raise HTTPException(404, f"file not found: {req.path}")
        _check_conflict(full, req.expected_hash)
        _backup(full)
        _atomic_write(full, req.content)
        upsert_one(get_cfg(), conn, req.path, kind="modified")
    return {"ok": True, "path": req.path}


@router.post("/file/create")
def file_create(req: CreateRequest, conn=Depends(get_conn)):
    with _lock_for(req.path):
        try:
            full = resolve_for_create(get_cfg(), req.path)
        except PathError as e:
            raise HTTPException(400, str(e))
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(full, "x", encoding="utf-8") as f:  # O_EXCL：绝不覆盖（P1-M2-2）
                f.write(req.content)
        except FileExistsError:
            raise HTTPException(409, "目标文件已存在，禁止覆盖（请换文件名）")
        upsert_one(get_cfg(), conn, req.path, kind="created")
    return {"ok": True, "path": req.path}


@router.patch("/file/status")
def file_status(req: StatusRequest, conn=Depends(get_conn)):
    """更新 frontmatter 状态字段（P1-M2-3：原 key 是什么就更新什么；无字段默认 list）。"""
    import frontmatter as fm_lib

    with _lock_for(req.path):
        try:
            full = resolve_for_write(get_cfg(), req.path)
        except PathError as e:
            raise HTTPException(400, str(e))
        if not full.is_file():
            raise HTTPException(404, f"file not found: {req.path}")
        _check_conflict(full, req.expected_hash)
        raw_bytes = full.read_bytes()
        text = raw_bytes.decode("utf-8", errors="replace")
        post = fm_lib.loads(text)
        meta = post.metadata or {}
        if "状态" in meta:
            key = "状态"
        elif "status" in meta:
            key = "status"
        else:
            key = None
        if key is None:
            meta["状态"] = [req.status]  # 无原字段：默认 list（匹配模板风格）
        elif isinstance(meta[key], list):
            meta[key] = [req.status]
        elif isinstance(meta[key], str):
            meta[key] = req.status
        else:
            meta[key] = req.status
        post.metadata = meta
        _backup(full)
        _atomic_write(full, fm_lib.dumps(post))
        upsert_one(get_cfg(), conn, req.path, kind="modified")
    return {"ok": True, "path": req.path, "status": req.status}


@router.get("/asset")
def get_asset(
    path: str = Query(..., description="图片或附件路径"),
    note_path: str | None = Query(None, description="引用该资源的笔记相对路径"),
):
    """安全提供 Vault 内图片与附件二进制流，支持根据当前笔记上下文相对解析 (P1-NEW-2 加固)。"""
    try:
        full = resolve_for_asset_read(get_cfg(), path, note_path)
    except PathError as e:
        raise HTTPException(400, str(e))
    media_type, _ = mimetypes.guess_type(str(full))
    # P1-NEW-2: 防止 MIME 混淆嗅探并限制主动脚本执行
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
    }
    return FileResponse(
        full, media_type=media_type or "application/octet-stream", headers=headers
    )
