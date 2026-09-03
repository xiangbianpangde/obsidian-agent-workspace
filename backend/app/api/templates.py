"""API: templates (列表、预览、基于模板创建笔记) (v0.2 §5, §6 / M4).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_conn
from ..scanner.vault_scanner import upsert_one
from ..security.path_guard import (
    PathError,
    resolve_for_create,
    resolve_for_template_read,
)
from ..state import get_cfg
from ..template.engine import inspect_template, render_template

router = APIRouter()


class CreateWithTemplateRequest(BaseModel):
    template_path: str
    title: str
    custom_path: str | None = None


@router.get("/templates")
def list_templates():
    """获取所有可用模板列表及分析信息。"""
    cfg = get_cfg()
    tpl_dir = cfg.templates_dir
    if not tpl_dir.is_dir():
        return {"templates": []}

    results = []
    for entry in sorted(tpl_dir.glob("*.md")):
        if entry.name.startswith("."):
            continue
        rel = entry.relative_to(cfg.vault_root).as_posix()
        try:
            raw = entry.read_text(encoding="utf-8", errors="replace")
            info = inspect_template(raw)
            results.append(
                {
                    "path": rel,
                    "name": entry.stem,
                    "filename": entry.name,
                    "supported_level": info["supported_level"],
                    "has_js_block": info["has_js_block"],
                    "has_file_move": info["has_file_move"],
                    "suggested_dir": info["suggested_dir"],
                }
            )
        except Exception:
            continue
    return {"templates": results}


@router.get("/template/preview")
def preview_template(
    path: str = Query(..., description="模板相对路径"),
    title: str = Query("新笔记", description="用户拟定标题"),
):
    """根据输入的标题实时预览模板渲染效果与目标路径建议。"""
    cfg = get_cfg()
    try:
        tpl_full = resolve_for_template_read(cfg, path)
    except PathError as e:
        raise HTTPException(400, str(e))

    raw = tpl_full.read_text(encoding="utf-8", errors="replace")
    rendered, suggested_dir = render_template(raw, title=title)

    # 计算建议落位路径
    clean_title = title.strip() or "未命名笔记"
    if not clean_title.endswith(".md"):
        clean_title += ".md"

    if suggested_dir:
        suggested_path = f"{suggested_dir}/{clean_title}"
    else:
        suggested_path = clean_title

    return {
        "template_path": path,
        "title": title,
        "rendered": rendered,
        "suggested_path": suggested_path,
        "has_js_block": "<%*" in raw,
    }


@router.post("/file/create-with-template")
def create_with_template(req: CreateWithTemplateRequest, conn=Depends(get_conn)):
    """使用模板创建新笔记并落盘，自动刷新索引。"""
    cfg = get_cfg()
    try:
        tpl_full = resolve_for_template_read(cfg, req.template_path)
    except PathError as e:
        raise HTTPException(400, str(e))

    raw = tpl_full.read_text(encoding="utf-8", errors="replace")
    clean_title = req.title.strip()
    if not clean_title:
        raise HTTPException(400, "笔记标题不能为空")

    rendered, suggested_dir = render_template(raw, title=clean_title)

    # 确定目标文件路径
    if req.custom_path and req.custom_path.strip():
        target_path = req.custom_path.strip()
    elif suggested_dir:
        filename = clean_title if clean_title.endswith(".md") else f"{clean_title}.md"
        target_path = f"{suggested_dir}/{filename}"
    else:
        filename = clean_title if clean_title.endswith(".md") else f"{clean_title}.md"
        target_path = filename

    # 验证并保存目标文件（原子 O_EXCL 创建防覆盖）
    try:
        full = resolve_for_create(cfg, target_path)
    except PathError as e:
        raise HTTPException(400, str(e))

    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        with open(full, "x", encoding="utf-8") as f:
            f.write(rendered)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        raise HTTPException(409, f"目标文件已存在，禁止覆盖: {target_path}")

    # 立即更新索引
    upsert_one(cfg, conn, target_path, kind="created")

    return {
        "ok": True,
        "path": target_path,
        "content": rendered,
    }
