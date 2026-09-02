"""API: config / files (tree, content, save, create, status)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..config import AppConfig
from ..database import sqlite
from ..security.path_guard import PathError, resolve_in_vault, vault_relative
from ..scanner.parser import parse_markdown
from ..scanner.vault_scanner import upsert_one

router = APIRouter()

_statuses_conn = None
_cfg: AppConfig | None = None


def init_api(cfg: AppConfig, conn) -> None:
    global _cfg, _statuses_conn
    _cfg = cfg
    _statuses_conn = conn


def _conn():
    return _statuses_conn


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
    meta = {
        r["key"]: _meta_value(r)
        for r in conn.execute(
            "SELECT key, value, value_type FROM metadata WHERE file_id=? ORDER BY key",
            (row["id"],),
        )
    }
    return {
        "path": row["path"],
        "filename": row["filename"],
        "title": row["title"],
        "folder": row["folder"],
        "size": row["size"],
        "modified_at": row["modified_at"],
        "hash": row["hash"],
        "tags": tags,
        "statuses": meta.get("状态", []),
        "metadata": meta,
    }


def _meta_value(r) -> str | list:
    if r["value_type"] == "list":
        try:
            return json.loads(r["value"] or "[]")
        except Exception:
            return r["value"]
    return r["value"] or ""


def _read_disk_sha256(rel: str) -> str | None:
    try:
        full = resolve_in_vault(_cfg, rel)
        return hashlib.sha256(full.read_bytes()).hexdigest()
    except Exception:
        return None


def _check_conflict(rel: str, expected_hash: str) -> Path:
    """Sol M1-C / v0.2 P0-MUST-2: optimistic locking."""
    full = resolve_in_vault(_cfg, rel)
    if not full.is_file():
        raise HTTPException(404, f"file not found: {rel}")
    current = hashlib.sha256(full.read_bytes()).hexdigest()
    if current != expected_hash:
        raise HTTPException(
            409, "文件已被外部修改，请重新加载"
        )
    return full


def _backup(full: Path) -> None:
    """v0.2 §7.6: 保存前备份（同文件滚动 1 份）。"""
    backup_dir = _cfg.database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe = full.relative_to(_cfg.vault_root).as_posix().replace("/", "__")
    target = backup_dir / f"{hashlib.sha256(str(full).encode()).hexdigest()[:8]}_{safe}.bak"
    target.write_bytes(full.read_bytes())


# ---------- endpoints ----------

@router.get("/config")
def get_config():
    conn = _conn()
    s = sqlite.stats(conn)
    return {
        "vault_path": str(_cfg.vault_root),
        "templates_dir": str(_cfg.templates_dir),
        "stats": {
            "files": s["files"],
            "tags": s["tags"],
            "last_scan": s["last_scan"],
        },
    }


@router.get("/files/tree")
def files_tree():
    conn = _conn()
    rows = conn.execute("SELECT * FROM files ORDER BY path").fetchall()
    payloads = [_file_payload(r, conn) for r in rows]
    root: dict = {"name": "", "path": "", "type": "dir", "children": []}
    for f in payloads:
        parts = f["path"].split("/")
        node = root
        for i, part in enumerate(parts):
            dir_path = "/".join(parts[: i + 1])
            child = next((c for c in node["children"] if c["path"] == dir_path and c["type"] == "dir"), None)
            if i < len(parts) - 1:
                if child is None:
                    child = {"name": part, "path": dir_path, "type": "dir", "children": []}
                    node["children"].append(child)
                node = child
            else:
                cleaned = {k: v for k, v in f.items() if k != "folder"}
                node["children"].append({**cleaned, "type": "file"})
    return root


@router.get("/file/content")
def file_content(path: str = Query(...)):
    try:
        full = resolve_in_vault(_cfg, path)
    except PathError as e:
        raise HTTPException(400, str(e))
    if not full.is_file():
        raise HTTPException(404, f"file not found: {path}")
    raw = full.read_text(encoding="utf-8", errors="replace")
    parsed = parse_markdown(full, _cfg.vault_root)
    return {
        "path": path,
        "raw": raw,
        "hash": parsed.sha256,
        "tags": parsed.tags,
        "statuses": parsed.statuses,
        "metadata": {k: json.loads(v[0]) if v[1] == "list" else v[0] for k, v in parsed.metadata.items()},
    }


@router.post("/file/save")
def file_save(req: SaveRequest):
    try:
        full = resolve_in_vault(_cfg, req.path)
    except PathError as e:
        raise HTTPException(400, str(e))
    _check_conflict(req.path, req.expected_hash)
    _backup(full)
    full.write_text(req.content, encoding="utf-8")
    upsert_one(_cfg, _conn(), req.path, kind="modified")
    return {"ok": True, "path": req.path}


@router.post("/file/create")
def file_create(req: CreateRequest):
    try:
        full = resolve_in_vault(_cfg, req.path)
    except PathError as e:
        raise HTTPException(400, str(e))
    if full.exists():
        raise HTTPException(409, "目标文件已存在，禁止覆盖（请换文件名）")
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(req.content, encoding="utf-8")
    upsert_one(_cfg, _conn(), req.path, kind="created")
    return {"ok": True, "path": req.path}


@router.patch("/file/status")
def file_status(req: StatusRequest):
    """更新 frontmatter 状态字段（尊重原字段类型：list 保持 list，scalar 保持 scalar）。"""
    import frontmatter as fm_lib

    try:
        full = resolve_in_vault(_cfg, req.path)
    except PathError as e:
        raise HTTPException(400, str(e))
    _check_conflict(req.path, req.expected_hash)
    raw = full.read_text(encoding="utf-8", errors="replace")
    post = fm_lib.loads(raw)
    meta = post.metadata or {}
    original = meta.get("状态", meta.get("status"))
    if isinstance(original, list):
        meta["状态"] = [req.status]
    elif isinstance(original, str) and original:
        meta["状态"] = req.status
    else:
        # 无原字段：写标量（用户模板默认列表时前端应显式传 mode）
        meta["状态"] = req.status
    post.metadata = meta
    _backup(full)
    full.write_text(fm_lib.dumps(post), encoding="utf-8")
    upsert_one(_cfg, _conn(), req.path, kind="modified")
    return {"ok": True, "path": req.path, "status": req.status}
