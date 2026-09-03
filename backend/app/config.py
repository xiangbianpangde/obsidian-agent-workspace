"""Configuration loader: config.yaml (project root)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # backend/ -> project root


@dataclass
class AppConfig:
    vault_path: Path
    templates_dir: Path
    database_path: Path
    bind_host: str
    port: int
    scan_exclude: list[str] = field(default_factory=list)
    extension_ignore: list[str] = field(default_factory=list)
    reject_symlink_escape: bool = True
    watchdog_enabled: bool = True
    debounce_ms: int = 500
    agentsview_db_path: Path | None = None
    agentsview_cli_path: Path | None = None
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        # 统一规范化路径，避免 macOS /var -> /private/var 前缀不一致
        self.vault_path = self.vault_path.resolve()
        self.templates_dir = self.templates_dir.resolve()
        self.database_path = self.database_path.resolve()
        if self.agentsview_db_path:
            self.agentsview_db_path = self.agentsview_db_path.expanduser().resolve()
        if self.agentsview_cli_path:
            self.agentsview_cli_path = self.agentsview_cli_path.expanduser().resolve()

    @property
    def vault_root(self) -> Path:
        return self.vault_path


def load_config(path: Path | str | None = None) -> AppConfig:
    p = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if not p.exists():
        raise FileNotFoundError(f"config.yaml not found at {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    vault_cfg = raw.get("vault", {}) or {}
    vault_path = Path(vault_cfg.get("path") or "").expanduser()
    if not vault_path.is_absolute():
        vault_path = p.parent / vault_path
    if not vault_path.exists():
        raise FileNotFoundError(f"vault.path does not exist: {vault_path}")

    tpl_cfg = raw.get("templates", {}) or {}
    templates_dir = Path(tpl_cfg.get("dir") or "资料库/模版")
    if not templates_dir.is_absolute():
        templates_dir = vault_path / templates_dir
    templates_dir = templates_dir.resolve()  # NFD/NFC 归一化（macOS）

    idx_cfg = raw.get("index", {}) or {}
    db_path = Path(idx_cfg.get("database") or "./data/vault.db")
    if not db_path.is_absolute():
        db_path = p.parent / db_path

    sec = raw.get("security", {}) or {}
    srv = raw.get("server", {}) or {}
    wd = raw.get("watchdog", {}) or {}
    av = raw.get("agentsview", {}) or {}

    av_db = Path(av.get("database")).expanduser() if av.get("database") else None
    av_cli = Path(av.get("cli_path")).expanduser() if av.get("cli_path") else None

    return AppConfig(
        vault_path=vault_path,
        templates_dir=templates_dir,
        database_path=db_path,
        bind_host=sec.get("bind_host", "127.0.0.1"),
        port=int(srv.get("port", 8787)),
        scan_exclude=list(sec.get("scan_exclude", [])),
        extension_ignore=list(sec.get("extension_ignore", [])),
        reject_symlink_escape=bool(sec.get("reject_symlink_escape", True)),
        watchdog_enabled=bool(wd.get("enabled", True)),
        debounce_ms=int(wd.get("debounce_ms", 500)),
        agentsview_db_path=av_db,
        agentsview_cli_path=av_cli,
        raw=raw,
    )


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
