"""API: tags（标签统计与按标签筛选，按状态分组）。
P1-M2-3: Python 侧组装（消除 N+1），status 统一识别 `状态`/`status` 两 key，list 先 decode 再分组。"""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database.sqlite import STATUS_KEYS
from ..deps import get_conn
from ..status import pick_status

router = APIRouter()


@router.get("/tags")
def tags_overview(conn=Depends(get_conn)):
    """标签统计 + 每个标签下的状态分布。"""
    tag_rows = conn.execute(
        """
        SELECT t.id, t.name AS tag, COUNT(*) AS cnt
        FROM tags t JOIN file_tags ft ON ft.tag_id = t.id
        GROUP BY t.id ORDER BY cnt DESC, t.name
        """
    ).fetchall()

    # 一次性拉取全部 (tag_id, file_id, key, value, value_type)（避免 N+1）
    status_rows = conn.execute(
        f"""
        SELECT ft.tag_id, ft.file_id, m.key, m.value, m.value_type
        FROM file_tags ft
        JOIN metadata m ON m.file_id = ft.file_id AND m.key IN ({','.join('?' for _ in STATUS_KEYS)})
        """,
        tuple(STATUS_KEYS),
    ).fetchall()

    by_file: dict[int, list] = {}
    tag_file_pairs: set[tuple[int, int]] = set()
    for r in status_rows:
        by_file.setdefault(r["file_id"], []).append(r)
        tag_file_pairs.add((r["tag_id"], r["file_id"]))

    tag_status: dict[int, Counter] = {}
    for tag_id, file_id in tag_file_pairs:
        picked = pick_status(by_file.get(file_id, []))
        if picked is None:
            continue
        _, value = picked
        values = value if isinstance(value, list) else [value]
        for v in values:
            tag_status.setdefault(tag_id, Counter())[v] += 1

    result = []
    for t in tag_rows:
        dist = tag_status.get(t["id"], Counter())
        result.append(
            {"tag": t["tag"], "count": t["cnt"], "status_distribution": dict(dist)}
        )
    return {"tags": result}


@router.get("/files/by-tag")
def files_by_tag(tag: str = Query(...), conn=Depends(get_conn)):
    """某标签下文件，按状态分组。"""
    row = conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()
    if not row:
        raise HTTPException(404, f"tag not found: {tag}")
    files = conn.execute(
        """
        SELECT f.* FROM files f
        JOIN file_tags ft ON ft.file_id = f.id
        WHERE ft.tag_id = ? ORDER BY f.folder, f.filename
        """,
        (row["id"],),
    ).fetchall()

    # 一次性拉取这些文件的状态 metadata
    ids = [f["id"] for f in files]
    meta_by_file: dict[int, list] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for m in conn.execute(
            f"""
            SELECT file_id, key, value, value_type FROM metadata
            WHERE file_id IN ({placeholders}) AND key IN ({','.join('?' for _ in STATUS_KEYS)})
            """,
            (*ids, *STATUS_KEYS),
        ):
            meta_by_file.setdefault(m["file_id"], []).append(m)

    grouped: dict[str, list[dict]] = {}
    for f in files:
        picked = pick_status(meta_by_file.get(f["id"], []))
        if picked is None:
            status = "无状态"
        else:
            value = picked[1]
            status = ", ".join(value) if isinstance(value, list) else str(value) or "无状态"
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
