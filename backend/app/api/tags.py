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


@router.get("/statuses")
def statuses_overview(conn=Depends(get_conn)):
    """状态中心：统计知识库中所有工作流状态及其文件数与关联 tags。"""
    # 1. 查询所有笔记及其状态
    meta_rows = conn.execute(
        f"""
        SELECT file_id, key, value, value_type FROM metadata
        WHERE key IN ({','.join('?' for _ in STATUS_KEYS)})
        """,
        tuple(STATUS_KEYS),
    ).fetchall()

    by_file: dict[int, list] = {}
    for r in meta_rows:
        by_file.setdefault(r["file_id"], []).append(r)

    # 2. 查询所有 (file_id, tag_name)
    ft_rows = conn.execute(
        """
        SELECT ft.file_id, t.name AS tag_name
        FROM file_tags ft
        JOIN tags t ON t.id = ft.tag_id
        """
    ).fetchall()
    tags_by_file: dict[int, list[str]] = {}
    for r in ft_rows:
        tags_by_file.setdefault(r["file_id"], []).append(r["tag_name"])

    # 3. 统计状态
    all_files = conn.execute("SELECT id FROM files").fetchall()
    status_counts: Counter[str] = Counter()
    status_tags: dict[str, Counter[str]] = {}

    for f in all_files:
        fid = f["id"]
        picked = pick_status(by_file.get(fid, []))
        if picked is None:
            st_list = ["无状态"]
        else:
            v = picked[1]
            st_list = v if isinstance(v, list) else [str(v)]
            if not st_list or st_list == [""]:
                st_list = ["无状态"]

        ftags = tags_by_file.get(fid, [])
        for st in st_list:
            status_counts[st] += 1
            st_counter = status_tags.setdefault(st, Counter())
            for tg in ftags:
                st_counter[tg] += 1

    # 按数量排序返回
    result = []
    # 常见核心工作流状态置顶
    order_pref = ["进行中", "已完成", "未整理", "已整理", "未开始", "无状态"]
    sorted_statuses = sorted(
        status_counts.keys(),
        key=lambda s: (
            order_pref.index(s) if s in order_pref else 99,
            -status_counts[s],
        ),
    )

    for st in sorted_statuses:
        top_tags = [
            {"tag": t, "count": c}
            for t, c in status_tags.get(st, Counter()).most_common(8)
        ]
        result.append(
            {
                "status": st,
                "count": status_counts[st],
                "top_tags": top_tags,
            }
        )

    return {"statuses": result}


@router.get("/files/by-status")
def files_by_status(status: str = Query(...), conn=Depends(get_conn)):
    """获取指定状态下的所有笔记。"""
    # 获取所有笔记及其状态
    meta_rows = conn.execute(
        f"""
        SELECT file_id, key, value, value_type FROM metadata
        WHERE key IN ({','.join('?' for _ in STATUS_KEYS)})
        """,
        tuple(STATUS_KEYS),
    ).fetchall()
    by_file: dict[int, list] = {}
    for r in meta_rows:
        by_file.setdefault(r["file_id"], []).append(r)

    # 匹配对应 status 的 file_ids
    matched_ids = []
    all_files = conn.execute("SELECT * FROM files ORDER BY modified_at DESC").fetchall()
    for f in all_files:
        fid = f["id"]
        picked = pick_status(by_file.get(fid, []))
        if picked is None:
            cur_statuses = ["无状态"]
        else:
            v = picked[1]
            cur_statuses = v if isinstance(v, list) else [str(v)]
            if not cur_statuses or cur_statuses == [""]:
                cur_statuses = ["无状态"]
        if status in cur_statuses:
            matched_ids.append(f)

    # 单次批量查询所有命中笔记的 tags (消除 N+1，Sol P2)
    matched_fids = [f["id"] for f in matched_ids]
    tags_map: dict[int, list[str]] = {}
    if matched_fids:
        placeholders = ",".join("?" for _ in matched_fids)
        rows = conn.execute(
            f"""
            SELECT ft.file_id, t.name
            FROM file_tags ft
            JOIN tags t ON t.id = ft.tag_id
            WHERE ft.file_id IN ({placeholders})
            ORDER BY t.name
            """,
            tuple(matched_fids),
        ).fetchall()
        for r in rows:
            tags_map.setdefault(r["file_id"], []).append(r["name"])

    results = []
    for f in matched_ids:
        results.append(
            {
                "path": f["path"],
                "title": f["title"],
                "filename": f["filename"],
                "folder": f["folder"],
                "modified_at": f["modified_at"],
                "hash": f["hash"],
                "tags": tags_map.get(f["id"], []),
            }
        )

    return {"status": status, "files": results}
