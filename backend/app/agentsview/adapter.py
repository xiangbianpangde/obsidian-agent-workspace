"""AgentsView Adapter (v0.2 / 第二个 P0).
提供对 agentsview 会话数据（~/.agentsview/sessions.db）的严格只读访问。
安全边界：
- 严格只读模式: mode=ro (严禁 immutable=1, Sol 修正)
- PRAGMA query_only=ON; PRAGMA busy_timeout=2000;
- 短生命周期连接，按需获取，立即释放，不阻塞 WAL checkpoint。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import AppConfig


class AgentsViewError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AgentsViewAdapter:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.db_path = cfg.agentsview_db_path or (
            Path.home() / ".agentsview" / "sessions.db"
        )
        self.cli_path = cfg.agentsview_cli_path or (
            Path.home() / ".local" / "bin" / "agentsview"
        )

    def _get_ro_connection(self) -> sqlite3.Connection:
        """获取短生命周期的严格只读连接 (P1-M2/P0 安全底线)。"""
        if not self.db_path.exists():
            raise AgentsViewError(
                "AGENTSVIEW_UNAVAILABLE",
                f"AgentsView 数据库不存在: {self.db_path}",
                status_code=503,
            )
        resolved_path = self.db_path.resolve()
        # 严格使用 mode=ro，坚决不加 immutable=1 (Sol 强调)
        uri = f"file:{resolved_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON;")
            conn.execute("PRAGMA busy_timeout=2000;")
            return conn
        except sqlite3.OperationalError as e:
            raise AgentsViewError(
                "AGENTSVIEW_BUSY", f"连接 AgentsView 只读数据库超时或忙: {e}", 503
            ) from None

    def get_version(self) -> str:
        """获取 agentsview CLI 版本号。"""
        if self.cli_path.exists() and os_access(self.cli_path):
            try:
                res = subprocess.run(
                    [str(self.cli_path), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                    check=False,
                )
                if res.returncode == 0:
                    out = res.stdout.strip()
                    # 匹配类似 agentsview v0.40.1 或 0.40.1
                    parts = out.split()
                    for p in parts:
                        if p.startswith("v") or (p and p[0].isdigit()):
                            return p
                    return out
            except Exception:
                pass
        return "v0.40.1 (local db)"

    def get_status(self) -> dict[str, Any]:
        """探测 agentsview 连通性、版本与基本计数。"""
        conn = self._get_ro_connection()
        try:
            sessions_count = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
            messages_count = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
            last_sess = conn.execute(
                "SELECT started_at FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            last_activity = last_sess["started_at"] if last_sess else None
        finally:
            conn.close()

        return {
            "ok": True,
            "version": self.get_version(),
            "transport": "sqlite-ro",
            "database_path": str(self.db_path),
            "session_count": sessions_count,
            "message_count": messages_count,
            "last_activity_at": last_activity,
        }

    def get_overview(self) -> dict[str, Any]:
        """会话全景看板：Agent 矩阵、项目排行、最近活跃度与最近会话。"""
        conn = self._get_ro_connection()
        try:
            # 1. Agent 统计矩阵 (按数量降序)
            agent_rows = conn.execute(
                "SELECT agent, COUNT(*) c, SUM(message_count) m FROM sessions GROUP BY agent ORDER BY c DESC"
            ).fetchall()
            agent_matrix = [
                {"agent": r["agent"], "session_count": r["c"], "message_count": r["m"] or 0}
                for r in agent_rows
            ]

            # 2. 活跃项目排行榜 (Top 12)
            project_rows = conn.execute(
                "SELECT project, COUNT(*) c FROM sessions GROUP BY project ORDER BY c DESC LIMIT 12"
            ).fetchall()
            project_ranking = [{"project": r["project"], "count": r["c"]} for r in project_rows]

            # 3. 时间维度活跃度统计 (近 24 小时、近 7 天)
            now = datetime.now(timezone.utc)
            t_24h = (now - timedelta(days=1)).isoformat()
            t_7d = (now - timedelta(days=7)).isoformat()

            recent_24h = conn.execute(
                "SELECT COUNT(*) c FROM sessions WHERE started_at >= ?", (t_24h,)
            ).fetchone()["c"]
            recent_7d = conn.execute(
                "SELECT COUNT(*) c FROM sessions WHERE started_at >= ?", (t_7d,)
            ).fetchone()["c"]

            # 4. 最近活跃的 8 场会话
            recent_sess_rows = conn.execute(
                """
                SELECT id, project, agent, first_message, display_name, session_name,
                       started_at, ended_at, message_count, user_message_count, cwd, git_branch
                FROM sessions ORDER BY started_at DESC LIMIT 8
                """
            ).fetchall()
            recent_sessions = [_format_session_row(r) for r in recent_sess_rows]

            total_sessions = sum(a["session_count"] for a in agent_matrix)
            total_messages = sum(a["message_count"] for a in agent_matrix)
        finally:
            conn.close()

        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "recent_24h_count": recent_24h,
            "recent_7d_count": recent_7d,
            "agent_matrix": agent_matrix,
            "project_ranking": project_ranking,
            "recent_sessions": recent_sessions,
        }

    def list_sessions(
        self,
        *,
        agent: str | None = None,
        project: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """有界分页查询会话列表。"""
        limit = max(1, min(limit, 100))  # 安全限幅 1~100
        offset = max(0, offset)

        conn = self._get_ro_connection()
        try:
            where_clauses = ["1=1"]
            params: list[Any] = []

            if agent and agent.strip() and agent.strip().lower() != "all":
                where_clauses.append("agent = ?")
                params.append(agent.strip())

            if project and project.strip() and project.strip().lower() != "all":
                where_clauses.append("project = ?")
                params.append(project.strip())

            if query and query.strip():
                where_clauses.append(
                    "(first_message LIKE ? OR display_name LIKE ? OR session_name LIKE ?)"
                )
                kw = f"%{query.strip()}%"
                params.extend([kw, kw, kw])

            where_str = " AND ".join(where_clauses)

            # 查询总数
            total = conn.execute(
                f"SELECT COUNT(*) c FROM sessions WHERE {where_str}", tuple(params)
            ).fetchone()["c"]

            # 分页查询
            sql = f"""
                SELECT id, project, agent, first_message, display_name, session_name,
                       started_at, ended_at, message_count, user_message_count, cwd, git_branch
                FROM sessions
                WHERE {where_str}
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(sql, (*params, limit, offset)).fetchall()
            sessions = [_format_session_row(r) for r in rows]
        finally:
            conn.close()

        return {
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + len(sessions)) < total,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        """查询单场会话的元数据详情。"""
        conn = self._get_ro_connection()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                raise AgentsViewError("SESSION_NOT_FOUND", f"会话未找到: {session_id}", 404)
            data = _format_session_row(row)
        finally:
            conn.close()
        return data

    def get_messages(
        self, session_id: str, from_ordinal: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """有界分页查询会话消息流 (禁止全量 dump，Sol 强调)。"""
        limit = max(1, min(limit, 100))
        from_ordinal = max(0, from_ordinal)

        conn = self._get_ro_connection()
        try:
            total = conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()["c"]

            rows = conn.execute(
                """
                SELECT ordinal, role, content, timestamp, model, thinking_text
                FROM messages
                WHERE session_id = ? AND ordinal >= ?
                ORDER BY ordinal ASC
                LIMIT ?
                """,
                (session_id, from_ordinal, limit),
            ).fetchall()

            messages = [
                {
                    "ordinal": r["ordinal"],
                    "role": r["role"],
                    "content": r["content"] or "",
                    "timestamp": r["timestamp"],
                    "model": r["model"] or "",
                    "thinking_text": r["thinking_text"] or "",
                }
                for r in rows
            ]
            next_ordinal = (
                messages[-1]["ordinal"] + 1 if messages else from_ordinal
            )
            has_more = (from_ordinal + len(messages)) < total
        finally:
            conn.close()

        return {
            "session_id": session_id,
            "messages": messages,
            "from_ordinal": from_ordinal,
            "limit": limit,
            "total": total,
            "next_ordinal": next_ordinal,
            "has_more": has_more,
        }

    def get_tool_calls(self, session_id: str) -> dict[str, Any]:
        """查询指定会话的工具调用明细。"""
        conn = self._get_ro_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, tool_name, category, input_json, result_content
                FROM tool_calls
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()

            calls = []
            for r in rows:
                calls.append(
                    {
                        "id": r["id"],
                        "tool_name": r["tool_name"],
                        "category": r["category"],
                        "input_json": r["input_json"],
                        "result_content": (r["result_content"] or "")[:2000],
                    }
                )
        finally:
            conn.close()

        return {"session_id": session_id, "tool_calls": calls, "count": len(calls)}


def os_access(p: Path) -> bool:
    import os
    return os.access(str(p), os.X_OK)


def _format_session_row(r) -> dict[str, Any]:
    title = r["display_name"] or r["session_name"] or r["first_message"] or "未命名会话"
    # 清理换行符作为标题展示
    clean_title = title.splitlines()[0][:120].strip() if title else "未命名会话"
    return {
        "id": r["id"],
        "project": r["project"],
        "agent": r["agent"],
        "title": clean_title,
        "first_message": (r["first_message"] or "")[:300],
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
        "message_count": r["message_count"],
        "user_message_count": r["user_message_count"],
        "cwd": r["cwd"] if "cwd" in r.keys() else None,
        "git_branch": r["git_branch"] if "git_branch" in r.keys() else None,
    }
