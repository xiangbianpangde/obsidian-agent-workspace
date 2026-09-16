"""一次性 EXPLAIN QUERY PLAN 审查：时间线/通道/文件树查询是否命中索引。

对生产库副本执行（vault.db 与 im_hub.db 复制到临时目录），不触碰原库。
SQL 全部直接内联在各 execute 调用中，值用 ? 绑定。
用法：.venv/bin/python scripts/explain_plans.py
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def copy_db(src: Path, dst_dir: Path) -> Path:
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    for ext in ("-wal", "-shm"):
        s = Path(str(src) + ext)
        if s.exists():
            shutil.copy2(s, Path(str(dst) + ext))
    return dst


def explain_im(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"===== im_hub.db（消息 {n} 条）=====")
    print("[messages 现有索引]")
    for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='messages';"):
        print(f"    {r[0]}")

    print("\n--- 时间线全量分页（无过滤）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM messages ORDER BY occurred_at_epoch_ms DESC, ingest_seq DESC LIMIT 51;"
    ):
        print(f"    -> {row[3]}")

    print("\n--- 时间线按平台过滤")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE source = ? ORDER BY occurred_at_epoch_ms DESC, ingest_seq DESC LIMIT 51;",
        ("wechat",),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 时间线按通道过滤")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE channel_id = ? ORDER BY occurred_at_epoch_ms DESC, ingest_seq DESC LIMIT 51;",
        ("wechat:filehelper",),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 时间线 keyset 翻页")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE channel_id = ? AND occurred_at_epoch_ms < ? ORDER BY occurred_at_epoch_ms DESC, ingest_seq DESC LIMIT 51;",
        ("wechat:filehelper", 1700000000000),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 重放查询（query_replay_events）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE ingest_seq > ? ORDER BY ingest_seq ASC LIMIT 200;",
        (0,),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 快照分页（query_snapshot_page）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE ingest_seq > ? AND ingest_seq <= ? ORDER BY ingest_seq ASC LIMIT 50;",
        (0, 99999),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 通道列表（list_channels）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM channels ORDER BY last_occurred_at_epoch_ms DESC;"
    ):
        print(f"    -> {row[3]}")


def explain_vault(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    print(f"===== vault.db（文件 {n} 条）=====")
    print("[files 表关键列]")
    for c in conn.execute("PRAGMA table_info(files);"):
        print(f"    {c[1]} {c[2]} pk={c[5]}")

    print("\n--- 文件树（files_tree）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM files ORDER BY path;"
    ):
        print(f"    -> {row[3]}")

    print("\n--- 单文件标签 join（_file_payload）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT t.name FROM tags t JOIN file_tags ft ON ft.tag_id=t.id WHERE ft.file_id=? ORDER BY t.name;",
        (1,),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 元数据读取")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT key, value, value_type FROM metadata WHERE file_id=?;",
        (1,),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 路径定位（upsert_file）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM files WHERE path=?;",
        ("某笔记.md",),
    ):
        print(f"    -> {row[3]}")

    print("\n--- 标签热度统计（tags API）")
    for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT t.name, COUNT(*) FROM tags t JOIN file_tags ft ON ft.tag_id=t.id GROUP BY t.id ORDER BY 2 DESC LIMIT 20;"
    ):
        print(f"    -> {row[3]}")


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="explain_plans_"))
    print(f"工作目录: {tmpdir}")

    im_src = Path.home() / ".personal-ai-workspace" / "im" / "im_hub.db"
    if im_src.exists():
        conn = sqlite3.connect(str(copy_db(im_src, tmpdir)))
        print()
        explain_im(conn)
        conn.close()
    else:
        print("\n[跳过 im_hub.db：文件不存在]")

    vault_src = PROJECT_ROOT / "data" / "vault.db"
    if vault_src.exists():
        conn = sqlite3.connect(str(copy_db(vault_src, tmpdir)))
        print()
        explain_vault(conn)
        conn.close()
    else:
        print("\n[跳过 vault.db：文件不存在]")


if __name__ == "__main__":
    main()
