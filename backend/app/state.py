"""App 运行时状态（config 单例）。"""
from __future__ import annotations

from .config import AppConfig

_state: dict = {}


def init_state(cfg: AppConfig) -> None:
    _state["cfg"] = cfg


def get_cfg() -> AppConfig:
    try:
        return _state["cfg"]
    except KeyError:
        raise RuntimeError("app state not initialized (lifespan?)") from None


def get_db_path():
    return get_cfg().database_path
