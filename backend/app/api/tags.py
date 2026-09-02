"""API: tags（标签统计与按标签筛选，按状态分组）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..database import sqlite

router = APIRouter()

_conn = None


def init_api(conn) -> None:
    global _conn
    _conn = conn


@router.get("/tags")
def tags_overview():
    """标签统计 + 每个标签下的状态分布（v0.2 §6 GET /api/tags）。"""
    rows = _conn.execute(
        """
        SELECT t.name AS tag, COUNT(*) AS cnt
        FROM tags t JOIN file_tags ft ON ft.tag_id = t.id
        GROUP BY t.id ORDER BY cnt DESC, t.name
        """
    ).fetchall()
    result = []
    for r in rows:
        statuses = {
            s["value"]: s["cnt"]
            for s in _conn.execute(
                """
                SELECT m.value AS value, COUNT(*) AS cnt
                FROM file_tags ft
                JOIN files f ON f.id = ft.file_id
                JOIN metadata m ON m.file_id = f.id AND m.key = '状态'
                WHERE ft.tag_id = (
                    SELECT id FROM tags WHERE name = ?
                )
                GROUP BY m.value
                """,
                (r["tag"],),
            )
        }
        result.append(
            {"tag": r["tag"], "count": r["cnt"], "status_distribution": statuses}
        )
    return {"tags": result}


@router.get("/files/by-tag")
def files_by_tag(tag: str = Query(...)):
    """某标签下文件，按状态分组（v0.2 §6）。"""
    row = _conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()
    if not row:
        raise HTTPException(404, f"tag not found: {tag}")
    files = _conn.execute(
        """
        SELECT f.* FROM files f
        JOIN file_tags ft ON ft.file_id = f.id
        WHERE ft.tag_id = ? ORDER BY f.folder, f.filename
        """,
        (row["id"],),
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for f in files:
        status = "无状态"
        m = _conn.execute(
            "SELECT value, value_type FROM metadata WHERE file_id=? AND key='状态'", (f["id"],)
        ).fetchone()
        if m:
            from ..api.files import _meta_value as mv

            v = mv(m)
            status = ", ".join(v) if isinstance(v, list) else str(v) or "无状态"
        grouped.setdefault(status, []).append(
            {
                "path": f["path"],
                "title": f["title"],
                "filename": f["filename"],
                "folder": f["folder"],
                "modified_at": f["modified_at"],
                "hash": f["hash"],
            }
        )
    return {"tag": tag, "groups": grouped}
