"""一次性性能 profile 脚本：全量索引实测 + EXPLAIN QUERY PLAN 索引审查。

对真实 vault 只读扫描，索引结果写入临时数据库（不触碰 data/vault.db 生产库）。
用法：.venv/bin/python scripts/profile_index.py
"""
from __future__ import annotations

import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import load_config  # noqa: E402
from backend.app.database import sqlite as sqlite_db  # noqa: E402
from backend.app.scanner.vault_scanner import scan_vault  # noqa: E402


def main() -> None:
    cfg = load_config()
    tmpdir = tempfile.mkdtemp(prefix="vault_profile_")
    tmp_db = Path(tmpdir) / "vault_profile.db"

    n_notes = sum(1 for p in cfg.vault_root.rglob("*") if p.suffix.lower() == ".md" and not p.name.startswith("."))
    print(f"vault: {cfg.vault_root}")
    print(f"markdown 笔记总数: {n_notes}")
    print(f"临时索引库: {tmp_db}\n")

    conn = sqlite_db.connect(tmp_db)

    # ---------- 全量索引实测（无 profiler 纯耗时）----------
    t0 = time.perf_counter()
    stats = scan_vault(cfg, conn)
    cold_s = time.perf_counter() - t0
    print(f"[1] 全量索引（无 profiler）: {cold_s:.2f}s, 索引 {stats['files_indexed']} 篇, "
          f"secret 跳过 {stats['secret_skipped']}, 官方宣称折算 {cold_s / max(stats['files_indexed'], 1) * 1000:.1f} ms/篇\n")

    # ---------- 冷启动 cProfile：重新建库 ----------
    conn.close()
    tmp_db.unlink()
    conn = sqlite_db.connect(tmp_db)

    profiler = cProfile.Profile()
    t1 = time.perf_counter()
    profiler.enable()
    stats2 = scan_vault(cfg, conn)
    profiler.disable()
    profiled_s = time.perf_counter() - t1

    s = io.StringIO()
    ps = pstats.Stats(profiler, stream=s).sort_stats("cumulative")
    ps.print_stats(22)
    print(f"[2] cProfile 全量索引: {profiled_s:.2f}s（profiler 开销内），Top 热点（累计）:")
    lines = s.getvalue().splitlines()
    for ln in lines[4:30]:
        print(ln)

    # 按内部耗时排一次，找自热点
    s2 = io.StringIO()
    pstats.Stats(profiler, stream=s2).sort_stats("tottime").print_stats(12)
    print("\n[3] Top 自耗时（tottime）:")
    for ln in s2.getvalue().splitlines()[4:20]:
        print(ln)

    conn.close()
    tmp_db.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
