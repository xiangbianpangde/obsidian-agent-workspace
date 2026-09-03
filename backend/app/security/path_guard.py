"""Path guard: operation-aware file access boundary (v0.2 §7 / P1-M2-4).

- 所有操作：resolve 后在 vault_root 内、拒绝 symlink escape
- read：仅 .md、命中 scan_exclude 拒绝、secret（当前内容重检或曾 secret_skipped）拒绝
- write/create：额外禁止 templates_dir（模板只读）

注意：macOS 文件名 NFD/NFC —— 所有路径比较统一 unicodedata.normalize('NFC', ...)。
"""
from __future__ import annotations

import unicodedata
from pathlib import Path

from ..config import AppConfig
from .secret_detector import looks_like_secret


class PathError(ValueError):
    pass


def _norm(path: Path | str) -> str:
    return unicodedata.normalize("NFC", str(path))


def is_within(root: Path, candidate: Path) -> bool:
    r, c = _norm(root), _norm(candidate)
    return c == r or c.startswith(r.rstrip("/") + "/")


def matches_scan_exclude(vault_root: Path, rel: str, exclude: list[str]) -> bool:
    """与 scanner 使用同一套排除规则（P1-M2-1 统一；原 scanner._excluded）。"""
    if not exclude:
        return False
    parts = Path(rel).parts if isinstance(rel, str) else rel
    norm_parts = [unicodedata.normalize("NFC", p) for p in parts]
    for token in exclude:
        t = unicodedata.normalize("NFC", token)
        if norm_parts and t in norm_parts:
            return True
        if t.startswith(".") and norm_parts:
            if any(p.startswith(".obsidian") or p == t for p in norm_parts):
                return True
    return False


def resolve_in_vault(cfg: AppConfig, relative_or_abs: str) -> Path:
    vault_root = cfg.vault_root
    cand = Path(relative_or_abs).expanduser()
    if not cand.is_absolute():
        cand = vault_root / cand
    resolved = cand.resolve(strict=False)
    if not is_within(vault_root, resolved):
        raise PathError(f"path escapes vault: {relative_or_abs}")
    if cfg.reject_symlink_escape and resolved.is_symlink():
        target = resolved.resolve(strict=True)
        if not is_within(vault_root, target):
            raise PathError(f"symlink escape rejected: {relative_or_abs}")
    return resolved


def _reject_excluded(cfg: AppConfig, rel: str) -> None:
    if matches_scan_exclude(cfg.vault_root, rel, cfg.scan_exclude):
        raise PathError(f"path in excluded zone: {rel}")


def _reject_non_note(path: Path) -> None:
    if path.suffix.lower() != ".md":
        raise PathError(f"only .md notes are accessible: {path.name}")


def _reject_secret(cfg: AppConfig, full: Path) -> None:
    """读取时重跑 secret detector（P1-M2-4）：即使 watchdog 尚未处理，也不返回 secret。"""
    try:
        raw = full.read_bytes()
    except OSError:
        return
    hit, note = looks_like_secret(raw.decode("utf-8", errors="replace"))
    if hit:
        raise PathError(f"file blocked by secret guard ({note}): {full.name}")


def resolve_for_read(cfg: AppConfig, relative_or_abs: str) -> Path:
    """note 读取边界：vault 内 + .md + 非排除区 + 非 secret。"""
    rel = Path(relative_or_abs).as_posix()
    _reject_excluded(cfg, rel)
    full = resolve_in_vault(cfg, rel)
    _reject_non_note(full)
    if full.exists():
        _reject_secret(cfg, full)
    return full


def resolve_for_write(cfg: AppConfig, relative_or_abs: str) -> Path:
    """note 写边界：vault 内 + .md + 非排除区（canonical 校验）+ 模板目录禁止写。"""
    full = resolve_in_vault(cfg, relative_or_abs)
    try:
        canonical_rel = full.relative_to(cfg.vault_root).as_posix()
    except ValueError:
        raise PathError(f"path escapes vault: {relative_or_abs}") from None
    _reject_excluded(cfg, canonical_rel)
    _reject_non_note(full)
    if is_within(cfg.templates_dir, full.absolute()):
        raise PathError(f"templates dir is read-only: {canonical_rel}")
    return full


def resolve_for_read_snapshot(cfg: AppConfig, relative_or_abs: str):
    """P1-M2-4 FINAL（MUST-2）：canonical resolve → canonical rel 检查 exclude → 单次 read_bytes
    → 同一份 bytes 跑 secret detector。返回 (full, raw_bytes)，hash/parse/response 全部复用同一 snapshot。"""
    full = resolve_in_vault(cfg, relative_or_abs)
    try:
        canonical_rel = full.relative_to(cfg.vault_root).as_posix()
    except ValueError:
        raise PathError(f"path escapes vault: {relative_or_abs}") from None
    _reject_excluded(cfg, canonical_rel)   # canonical 后检查（内部 symlink 不再绕开排除区）
    _reject_non_note(full)
    if not full.is_file():
        raise PathError(f"not a file: {canonical_rel}")
    raw_bytes = full.read_bytes()
    hit, note = looks_like_secret(raw_bytes.decode("utf-8", errors="replace"))
    if hit:
        raise PathError(f"file blocked by secret guard ({note}): {full.name}")
    return full, raw_bytes


def resolve_for_create(cfg: AppConfig, relative_or_abs: str) -> Path:
    """创建边界：write 边界（文件允许不存在）。"""
    return resolve_for_write(cfg, relative_or_abs)
