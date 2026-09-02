"""M1 CLI: 全量扫描 + 可选 watchdog 常驻。

用法:
  .venv/bin/python -m backend.scripts.scan            # 全量扫描并统计
  .venv/bin/python -m backend.scripts.scan --watch    # 扫描后启动 watchdog
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.database import sqlite  # noqa: E402
from app.scanner.vault_scanner import scan_vault  # noqa: E402


def main() -> None:
    cfg = load_config()
    conn = sqlite.connect(cfg.database_path)
    result = scan_vault(cfg, conn)
    s = result["stats"]
    last = s["last_scan"]
    print(
        f"scan done: files={result['files_indexed']} "
        f"tags={s['tags']} secret_skipped={result['secret_skipped']} "
        f"duration={result['duration_ms']}ms"
    )
    if "--watch" in sys.argv and cfg.watchdog_enabled:
        from app.watch.watcher import start_watcher

        observer = start_watcher(cfg, conn)
        print(f"watchdog on: {cfg.vault_root} (Ctrl-C 停止)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main()
