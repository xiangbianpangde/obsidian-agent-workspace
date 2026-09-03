"""API: templates (列表、预览、基于模板创建笔记) (v0.2 §5, §6 / M4 rev2).
P1-M4-1: 两阶段 render，保证 tp.file.path 正确填充。
P1-M4-2: resolve_for_template_read_snapshot 单次快照与 inline secret 拦截。
P1-M4-3: fail-closed 降级提示。
合同收口: 支持 vars 参数，clean_title 限制纯文件名。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_conn
from ..scanner.vault_scanner import upsert_one
from ..security.path_guard import (
    PathError,
    resolve_for_create,
    resolve_for_template_read_snapshot,
)
from ..state import get_cfg
from ..template.engine import (
    compute_target_path,
    inspect_template,
    render_template,
)

router = APIRouter()


class CreateWithTemplateRequest(BaseModel):
    template_path: str | None = None
    template: str | None = None  # 合同字段对齐 (v0.2 §6)
    title: str
    custom_path: str | None = None
    vars: dict[str, str] | None = None

    def get_template_path(self) -> str:
        p = self.template_path or self.template
        if not p:
            raise HTTPException(400, "必须提供 template_path 或 template 字段")
        return p


def _sanitize_title(title: str) -> str:
    # 限制 title 纯为文件名，禁止使用 / 或 \ 篡改建议目录
    clean = title.strip().replace("/", "_").replace("\\", "_")
    if not clean:
        raise HTTPException(400, "笔记标题不能为空")
    return clean


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
            # P1-M4-2: 读模板时经过 secret 检测
            _, raw_bytes = resolve_for_template_read_snapshot(cfg, rel)
            raw = raw_bytes.decode("utf-8", errors="replace")
            info = inspect_template(raw)
            results.append(
                {
                    "path": rel,
                    "name": entry.stem,
                    "filename": entry.name,
                    "supported_level": info["supported_level"],
                    "has_js_block": info["has_js_block"],
                    "has_unsupported_tags": info["has_unsupported_tags"],
                    "has_file_move": info["has_file_move"],
                    "suggested_dir": info["suggested_dir"],
                }
            )
        except PathError:
            # 模板若含 secret 则忽略不列入可用模板
            continue
        except Exception:
            continue
    return {"templates": results}


@router.get("/template/preview")
def preview_template(
    path: str = Query(..., description="模板相对路径"),
    title: str = Query("新笔记", description="用户拟定标题"),
):
    """根据输入的标题实时预览模板渲染效果与目标路径建议（P1-M4-1, P1-M4-2）。"""
    cfg = get_cfg()
    try:
        _, raw_bytes = resolve_for_template_read_snapshot(cfg, path)
    except PathError as e:
        raise HTTPException(400, str(e))

    raw = raw_bytes.decode("utf-8", errors="replace")
    clean_title = _sanitize_title(title)
    info = inspect_template(raw)

    # 先计算 suggested_path，作为 target_path (P1-M4-1)
    suggested_path = compute_target_path(clean_title, info["suggested_dir"])
    rendered, _ = render_template(raw, title=clean_title, target_path=suggested_path)

    return {
        "template_path": path,
        "title": clean_title,
        "rendered": rendered,
        "suggested_path": suggested_path,
        "has_js_block": info["has_js_block"],
        "supported_level": info["supported_level"],
    }


@router.post("/file/create-with-template")
def create_with_template(req: CreateWithTemplateRequest, conn=Depends(get_conn)):
    """使用模板创建新笔记并落盘，自动刷新索引（P1-M4-1 两阶段安全落盘）。"""
    cfg = get_cfg()
    tpl_path = req.get_template_path()
    try:
        _, raw_bytes = resolve_for_template_read_snapshot(cfg, tpl_path)
    except PathError as e:
        raise HTTPException(400, str(e))

    raw = raw_bytes.decode("utf-8", errors="replace")
    clean_title = _sanitize_title(req.title)
    info = inspect_template(raw)

    # 1. 确定最终目标文件路径 (两阶段阶段一: P1-M4-1)
    target_path = compute_target_path(
        clean_title, info["suggested_dir"], req.custom_path
    )

    # 2. 严格校验目标路径安全（非模板目录、非排除区、不逃逸）
    try:
        full = resolve_for_create(cfg, target_path)
    except PathError as e:
        raise HTTPException(400, str(e))

    # 3. 带真实 target_path 渲染模板内容 (两阶段阶段二: P1-M4-1)
    rendered, _ = render_template(
        raw, title=clean_title, target_path=target_path, vars=req.vars or {}
    )

    # 4. 原子 O_EXCL 创建防覆盖
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        with open(full, "x", encoding="utf-8") as f:
            f.write(rendered)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        raise HTTPException(409, f"目标文件已存在，禁止覆盖: {target_path}")

    # 5. 立即更新索引
    upsert_one(cfg, conn, target_path, kind="created")

    return {
        "ok": True,
        "path": target_path,
        "content": rendered,
    }
