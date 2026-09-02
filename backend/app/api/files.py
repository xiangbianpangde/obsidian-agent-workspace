"""API: config / files (tree, content, save, create, status).
P1-M2-2: 单次字节快照、per-path 锁、原子 replace、O_EXCL 创建。
P1-M2-3: status 语义统一。P1-M2-4: operation-aware 路径边界。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..database import sqlite
from ..deps import get_conn
from ..scanner.parser import parse_markdown
from ..scanner.vault_scanner import upsert_one
from ..security.path_guard import (
    PathError,
    resolve_for_create,
    resolve_for_read,
    resolve_for_write,
)
from ..state import get_cfg
from ..status import pick_status

router = APIRouter()

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _path_locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[path] = lock
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

def _file_payload(row, conn) -> dict:
    tags = [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN file_tags ft ON ft.tag_id=t.id WHERE ft.file_id=? ORDER BY t.name",
            (row["id"],),
        )
    ]
    meta_rows = conn.execute(
        "SELECT key, value, value_type FROM metadata WHERE file_id=?",
        (row["id"],),
    ).fetchall()
    status = pick_status(meta_rows)
    return {
        "path": row["path"],
        "filename": row["filename"],
        "title": row["title"],
        "folder": row["folder"],
        "size": row["size"],
        "modified_at": row["modified_at"],
        "hash": row["hash"],
        "tags": tags,
        "statuses": status[1] if status else [],
    }


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
    """原子替换（P1-M2-2）：同目录 temp + flush + os.replace。"""
    tmp = full.with_name(f".{full.name}.ws-tmp-{uuid.uuid4().hex}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
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
    rows = conn.execute("SELECT * FROM files ORDER BY path").fetchall()
    payloads = [_file_payload(r, conn) for r in rows]
    root: dict = {"name": "", "path": "", "type": "dir", "children": []}
    for f in payloads:
        parts = f["path"].split("/")
        node = root
        for i, part in enumerate(parts):
            dir_path = "/".join(parts[: i + 1])
            child = next(
                (c for c in node["children"] if c["path"] == dir_path and c["type"] == "dir"),
                None,
            )
            if i < len(parts) - 1:
                if child is None:
                    child = {"name": part, "path": dir_path, "type": "dir", "children": []}
                    node["children"].append(child)
                node = child
            else:
                node["children"].append({**f, "type": "file"})
    return root


@router.get("/file/content")
def file_content(path: str = Query(...), conn=Depends(get_conn)):
    try:
        full = resolve_for_read(get_cfg(), path)
    except PathError as e:
        raise HTTPException(400, str(e))
    if not full.is_file():
        raise HTTPException(404, f"file not found: {path}")
    # P1-M2-2: 单次字节快照 → hash → decode → parse（避免 raw/hash 版本不一致）
    raw_bytes = full.read_bytes()
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
