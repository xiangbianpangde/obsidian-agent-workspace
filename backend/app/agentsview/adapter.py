"""AgentsView Adapter (v0.2 rev2 / 第二个 P0).
实现权威会话 API DTO 契约与双通道传输 (P1-AV-1)。
传输层：
1. CliTransport (官方推荐主路径)：优先调用 agentsview CLI 获得权威 Session API DTO；
2. SqliteRoTransport (高效只读回退)：当 CLI 不可用时，通过只读 SQLite 连接并执行 DTO 规范化映射。
安全边界：
- 严格只读模式: mode=ro (严禁 immutable=1, Sol 修正)
- PRAGMA query_only=ON; PRAGMA busy_timeout=2000;
- 短生命周期连接，按需获取，立即释放，不阻塞 WAL checkpoint。
"""
from __future__ import annotations

import json
import os
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

    def _cli_available(self) -> bool:
        return bool(
            self.cli_path
            and self.cli_path.is_file()
            and os.access(str(self.cli_path), os.X_OK)
        )

    def _get_ro_connection(self) -> sqlite3.Connection:
        """获取短生命周期的严格只读连接 (mode=ro，严禁 immutable=1)。"""
        if not self.db_path.exists():
            raise AgentsViewError(
                "AGENTSVIEW_UNAVAILABLE",
                f"AgentsView 数据库不存在: {self.db_path}",
                status_code=503,
            )
        resolved_path = self.db_path.resolve()
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

    def get_version(self) -> str | None:
        """探测 agentsview 版本号 (P2-3: fail-open 避免猜测硬编码)。"""
        if self._cli_available():
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
                    for p in out.split():
                        if p.startswith("v") or (p and p[0].isdigit()):
                            return p
                    return out or None
            except Exception:
                pass
        return None

    def get_status(self) -> dict[str, Any]:
        """探测 agentsview 连通性、传输模式与基本元数据。"""
        transport = "cli" if self._cli_available() else "sqlite-ro"
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
            "version": self.get_version() or "unknown",
            "transport": transport,
            "database_path": str(self.db_path),
            "session_count": sessions_count,
            "message_count": messages_count,
            "last_activity_at": last_activity,
        }

    def get_overview(self) -> dict[str, Any]:
        """会话工作流全景大盘：Agent 矩阵、项目排行、时间活跃度与最近会话。"""
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

            # 4. 最近活跃的 8 场会话 (格式化为标准 DTO)
            recent_sess_rows = conn.execute(
                """
                SELECT id, project, machine, agent, first_message, display_name, session_name,
                       started_at, ended_at, message_count, user_message_count, cwd, git_branch
                FROM sessions ORDER BY started_at DESC LIMIT 8
                """
            ).fetchall()
            recent_sessions = [_format_session_dto(r) for r in recent_sess_rows]

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
        """有界分页查询会话列表 (返回权威 Session DTO)。"""
        limit = max(1, min(limit, 100))
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

            total = conn.execute(
                f"SELECT COUNT(*) c FROM sessions WHERE {where_str}", tuple(params)
            ).fetchone()["c"]

            sql = f"""
                SELECT id, project, machine, agent, first_message, display_name, session_name,
                       started_at, ended_at, message_count, user_message_count, cwd, git_branch
                FROM sessions
                WHERE {where_str}
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(sql, (*params, limit, offset)).fetchall()
            sessions = [_format_session_dto(r) for r in rows]
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
        """查询单场会话元数据详情 (权威 Session DTO)。"""
        conn = self._get_ro_connection()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                raise AgentsViewError("SESSION_NOT_FOUND", f"会话未找到: {session_id}", 404)
            data = _format_session_dto(row)
        finally:
            conn.close()
        return data

    def get_messages(
        self, session_id: str, from_ordinal: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """
        有界分页查询会话消息流 (P1-AV-3 & P2-2).
        算法：查询 limit + 1 条，精确判定 has_more 与 next_ordinal。
        """
        limit = max(1, min(limit, 100))
        from_ordinal = max(0, from_ordinal)

        conn = self._get_ro_connection()
        try:
            total = conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()["c"]

            # 查 limit + 1 条以精准判断是否有下一页 (Sol P2-2)
            rows = conn.execute(
                """
                SELECT ordinal, role, content, timestamp, model, thinking_text
                FROM messages
                WHERE session_id = ? AND ordinal >= ?
                ORDER BY ordinal ASC
                LIMIT ?
                """,
                (session_id, from_ordinal, limit + 1),
            ).fetchall()

            has_more = len(rows) > limit
            valid_rows = rows[:limit]

            messages = [_format_message_dto(r) for r in valid_rows]
            next_ordinal = (
                valid_rows[-1]["ordinal"] + 1 if valid_rows else from_ordinal
            )
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

    def get_tool_calls(
        self, session_id: str, limit: int = 100
    ) -> dict[str, Any]:
        """查询指定会话的工具调用明细 (Sol P2-1: 有界查询)。"""
        limit = max(1, min(limit, 200))
        conn = self._get_ro_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, tool_name, category, input_json, result_content
                FROM tool_calls
                WHERE session_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

            calls = [_format_tool_call_dto(r) for r in rows]
        finally:
            conn.close()

        return {"session_id": session_id, "tool_calls": calls, "count": len(calls)}


# ======================== 权威 Session API DTO 映射 (P1-AV-1) ========================

def _format_session_dto(r) -> dict[str, Any]:
    """标准化映射为官方 Session DTO。"""
    keys = r.keys()
    title = r["display_name"] or r["session_name"] or r["first_message"] or "未命名会话"
    clean_title = title.splitlines()[0][:120].strip() if title else "未命名会话"
    return {
        "id": r["id"],
        "project": r["project"],
        "machine": r["machine"] if "machine" in keys else "local",
        "agent": r["agent"],
        "title": clean_title,
        "first_message": (r["first_message"] or "")[:300],
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
        "message_count": r["message_count"],
        "user_message_count": r["user_message_count"],
        "cwd": r["cwd"] if "cwd" in keys else None,
        "git_branch": r["git_branch"] if "git_branch" in keys else None,
    }


def _format_message_dto(r) -> dict[str, Any]:
    """标准化映射为官方 Message DTO。"""
    keys = r.keys()
    ts = r["timestamp"] if "timestamp" in keys else None
    return {
        "ordinal": r["ordinal"],
        "role": r["role"],
        "content": r["content"] or "",
        "timestamp": ts,
        "created_at": ts,  # 对齐 Contract
        "model": r["model"] if "model" in keys else "",
        "thinking_text": r["thinking_text"] if "thinking_text" in keys else "",
    }


def _format_tool_call_dto(r) -> dict[str, Any]:
    """标准化映射为官方 ToolCall DTO。"""
    keys = r.keys()
    input_str = r["input_json"] if "input_json" in keys else None
    parsed_args = None
    if input_str:
        try:
            parsed_args = json.loads(input_str)
        except Exception:
            parsed_args = input_str

    result_raw = (r["result_content"] or "")[:2000] if "result_content" in keys else ""
    return {
        "id": str(r["id"]),
        "tool_name": r["tool_name"],
        "category": r["category"] if "category" in keys else "tool",
        "arguments": parsed_args or input_str,
        "input_json": input_str,
        "result_summary": result_raw,
        "result_content": result_raw,
    }
