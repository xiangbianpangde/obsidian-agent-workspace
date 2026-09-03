"""API: AgentsView AI 会话中心 (第二个 P0 / v0.2 rev2).
提供对 agentsview 会话数据与分析的严格只读端点。
P1-AV-2 隐私硬边界:
所有返回会话内容或元数据的端点统一注入 Cache-Control: no-store, no-cache, must-revalidate,
彻底阻断本地及代理层缓存任何用户提示词、代码或敏感输出。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from ..agentsview.adapter import AgentsViewAdapter, AgentsViewError
from ..state import get_cfg

router = APIRouter()

_adapter: AgentsViewAdapter | None = None

# P1-AV-2: 全量隐私防缓存响应头 (覆盖整套 AgentsView 路由)
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def get_adapter() -> AgentsViewAdapter:
    global _adapter
    if _adapter is None:
        cfg = get_cfg()
        _adapter = AgentsViewAdapter(cfg)
    return _adapter


@router.get("/status")
def agentsview_status():
    """探测 AgentsView 数据库连通性、版本与会话总量。"""
    try:
        data = get_adapter().get_status()
        return JSONResponse(data, headers=_NO_STORE_HEADERS)
    except AgentsViewError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message}) from None


@router.get("/overview")
def agentsview_overview():
    """工作流全景看板：Agent 矩阵、项目排行、近 24h / 近 7d 活跃指标。"""
    try:
        data = get_adapter().get_overview()
        return JSONResponse(data, headers=_NO_STORE_HEADERS)
    except AgentsViewError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message}) from None


@router.get("/sessions")
def list_sessions(
    agent: str | None = Query(None, description="按 Agent 过滤 (如 pi, claude, codex)"),
    project: str | None = Query(None, description="按项目名称过滤"),
    q: str | None = Query(None, description="搜索关键词"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """有界分页检索会话列表 (权威 Session DTO)。"""
    try:
        data = get_adapter().list_sessions(
            agent=agent, project=project, query=q, limit=limit, offset=offset
        )
        return JSONResponse(data, headers=_NO_STORE_HEADERS)
    except AgentsViewError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message}) from None


@router.get("/session/{session_id}")
def get_session_detail(session_id: str):
    """获取单场会话元数据（工作目录、Git 分支、起止时间、轮次）。"""
    try:
        data = get_adapter().get_session(session_id)
        return JSONResponse(data, headers=_NO_STORE_HEADERS)
    except AgentsViewError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message}) from None


@router.get("/session/{session_id}/messages")
def get_session_messages(
    session_id: str,
    from_ordinal: int = Query(0, ge=0, alias="from"),
    limit: int = Query(50, ge=1, le=100),
):
    """有界分页回溯会话消息流 (Read-Through Only, no-store, P1-AV-2/3)。"""
    try:
        data = get_adapter().get_messages(
            session_id, from_ordinal=from_ordinal, limit=limit
        )
        return JSONResponse(data, headers=_NO_STORE_HEADERS)
    except AgentsViewError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message}) from None


@router.get("/session/{session_id}/tool-calls")
def get_session_tool_calls(
    session_id: str,
    limit: int = Query(100, ge=1, le=200),
):
    """回溯会话中的工具调用明细 (Sol P2-1 有界 limit)。"""
    try:
        data = get_adapter().get_tool_calls(session_id, limit=limit)
        return JSONResponse(data, headers=_NO_STORE_HEADERS)
    except AgentsViewError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message}) from None
