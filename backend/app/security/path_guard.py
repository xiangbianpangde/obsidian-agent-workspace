"""Path guard: every file operation must pass here. No delete/rename API exists."""
from __future__ import annotations

from pathlib import Path

from ..config import AppConfig


class PathError(ValueError):
    pass


def resolve_in_vault(cfg: AppConfig, relative_or_abs: str) -> Path:
    """Resolve a vault-relative or absolute path; reject escapes and symlinks."""
    vault_root = cfg.vault_root
    cand = Path(relative_or_abs).expanduser()
    if not cand.is_absolute():
        cand = vault_root / cand
    resolved = cand.resolve(strict=False)

    if not (resolved == vault_root or resolved.is_relative_to(vault_root)):
        raise PathError(f"path escapes vault: {relative_or_abs}")

    # symlink escape: resolve() already canonicalizes; a symlink inside vault
    # pointing outside would resolve outside -> caught above. Extra guard:
    if cfg.reject_symlink_escape and resolved.is_symlink():
        target = resolved.resolve(strict=True)
        if not target.is_relative_to(vault_root):
            raise PathError(f"symlink escape rejected: {relative_or_abs}")
    return resolved


def vault_relative(cfg: AppConfig, path: Path) -> str:
    return path.relative_to(cfg.vault_root).as_posix()
