"""FastAPI 依赖：per-request SQLite connection（P1-M2-1：API 与 watchdog/scanner 各用独立连接）。"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends

from .database import sqlite
from .state import get_cfg


def get_conn() -> Iterator:
    cfg = get_cfg()
    conn = sqlite.connect(cfg.database_path)
    try:
        yield conn
    finally:
        conn.close()
