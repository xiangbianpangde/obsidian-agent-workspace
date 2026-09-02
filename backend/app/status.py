"""Status 语义统一层（P1-M2-3）：tree/tags/by-tag 共享。

- 两个 key：优先 `状态`，其次 `status`（避免双字段）
- list 值 decode 后按语义值处理（不按 JSON 字符串分组）
"""
from __future__ import annotations

import json

from .database.sqlite import STATUS_KEYS


def decode_status_value(value: str, value_type: str):
    if value_type == "list":
        try:
            v = json.loads(value or "[]")
            return v if isinstance(v, list) else [str(v)]
        except Exception:
            return [value]
    return value


def pick_status(meta_rows) -> tuple[str, str | list[str]] | None:
    """meta_rows: sqlite rows of (key, value, value_type)。返回 (key, decoded) 或 None。"""
    by_key = {}
    for r in meta_rows:
        by_key[r["key"]] = decode_status_value(r["value"], r["value_type"])
    for key in STATUS_KEYS:
        if key in by_key and by_key[key] not in (None, "", []):
            return key, by_key[key]
    for key in STATUS_KEYS:
        if key in by_key:
            return key, by_key[key]
    return None
