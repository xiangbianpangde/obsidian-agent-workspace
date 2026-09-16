"""Unified Vault write service.

ADR-007 requires every paper write to go through one component rather than
copying private helpers out of route handlers. It implements the invariants that
the existing ``files.py`` endpoints established, plus two that multi-file paper
writes specifically need:

1. **Zero delete** — no function here removes a file. Removal is expressed as
   metadata (``inactive_at`` / ``deleted_at``) written by callers.
2. **SHA256 optimistic lock** — an existing file may only be overwritten when the
   caller supplies the hash it last read. Anything else is a conflict.
3. **Pre-commit re-validation** — the in-process lock only serialises this
   process; it cannot lock Obsidian. So the target hash is checked a second time
   immediately before ``os.replace``, and the temporary file is discarded on
   mismatch. Portable filesystems offer no cross-process compare-and-swap, so
   this double check plus backup plus a conflict UI is the correct design.
4. **Versioned, deduplicated backups** — autosave can fire every few hundred
   milliseconds, so backups are content-addressed and time-bucketed instead of
   silently overwriting a single ``.bak``.
5. **New-hash responses** — every successful write returns the resulting hash so
   the client can keep autosaving without a re-read.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

__all__ = [
    "WriteError",
    "ConflictError",
    "PathRejected",
    "AlreadyExistsError",
    "WriteResult",
    "VaultWriteService",
]

#: Temp files created by this service. The scanner must ignore them.
TEMP_PREFIX = ".ws-paper-tmp-"


class WriteError(Exception):
    """Base class for write failures."""


class PathRejected(WriteError):
    """The requested path is outside the vault or in an excluded area."""


class ConflictError(WriteError):
    """The file changed underneath us since the caller last read it."""


class AlreadyExistsError(WriteError):
    """A no-clobber create targeted an existing file."""


@dataclass
class WriteResult:
    rel_path: str
    new_hash: str
    previous_hash: Optional[str]
    backup_path: Optional[str]
    created: bool


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VaultWriteService:
    """All Vault mutations for the paper subsystem funnel through here."""

    def __init__(
        self,
        vault_root: Path,
        backup_root: Optional[Path] = None,
        excluded_segments: Tuple[str, ...] = (
            ".obsidian",
            ".trash",
            ".git",
            "__pycache__",
        ),
    ):
        self.vault_root = Path(vault_root).expanduser().resolve(strict=False)
        self.backup_root = (
            Path(backup_root).expanduser()
            if backup_root
            else Path.home() / ".personal-ai-workspace" / "papers" / "backups"
        )
        self.excluded_segments = excluded_segments
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ paths
    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @staticmethod
    def _nfc(value: str) -> str:
        return unicodedata.normalize("NFC", value)

    def resolve(self, rel_path: str) -> Path:
        """Resolve a vault-relative path, refusing escapes and excluded areas.

        Writing is the dangerous direction, so this is stricter than a read
        resolution: absolute paths, ``..`` traversal, symlink escapes and
        excluded segments are all rejected outright.
        """
        raw = (rel_path or "").strip()
        if not raw:
            raise PathRejected("empty path")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", raw):
            raise PathRejected(f"absolute path rejected: {rel_path}")
        parts = Path(raw).parts
        if any(part == ".." for part in parts):
            raise PathRejected(f"path traversal rejected: {rel_path}")
        if any(part in self.excluded_segments for part in parts):
            raise PathRejected(f"path is in an excluded area: {rel_path}")

        candidate = (self.vault_root / raw).resolve(strict=False)
        try:
            candidate.relative_to(self.vault_root)
        except ValueError as exc:
            raise PathRejected(f"path escapes the vault: {rel_path}") from exc

        # Reject symlinks anywhere along the chain: a symlinked parent could
        # redirect the write outside the vault even though the path looks local.
        probe = candidate
        while probe != self.vault_root and probe.parent != probe:
            if probe.exists() and probe.is_symlink():
                raise PathRejected(f"symlink in path rejected: {rel_path}")
            probe = probe.parent
        return candidate

    def relpath(self, full: Path) -> str:
        return full.resolve(strict=False).relative_to(self.vault_root).as_posix()

    # ---------------------------------------------------------------- backups
    def _backup(self, full: Path, preimage: bytes) -> Optional[str]:
        """Content-addressed, time-bucketed backup of the preimage bytes.

        The preimage is the exact bytes the conflict check validated, so the
        backup always matches what was actually replaced.
        """
        try:
            rel = self.relpath(full)
        except ValueError:
            return None
        digest = sha256_bytes(preimage)
        # Bucket to the hour so a burst of autosaves keeps at most one copy per
        # content version instead of one per keystroke batch.
        bucket = f"{int(__import__('time').time()) // 3600}"
        safe = rel.replace("/", "__")
        target = self.backup_root / bucket / f"{digest[:16]}_{safe}.bak"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(preimage)
            os.replace(tmp, target)
            os.chmod(target, 0o600)
        return str(target)

    # ------------------------------------------------------------ atomic I/O
    def _write_atomic(self, full: Path, data: bytes) -> None:
        """Temp file in the same directory, fsync, rename, fsync the directory."""
        tmp = full.with_name(f"{TEMP_PREFIX}{uuid.uuid4().hex}")
        # Belt and braces: the temp file must also sit inside the vault.
        try:
            tmp.resolve(strict=False).relative_to(self.vault_root)
        except ValueError as exc:
            raise PathRejected("temp file would escape the vault") from exc

        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)

            # Preserve the existing mode; never silently reset permissions.
            if full.exists():
                os.chmod(tmp, full.stat().st_mode & 0o777)
            os.replace(tmp, full)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

        dir_fd = os.open(full.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # ---------------------------------------------------------------- public
    def read(self, rel_path: str) -> Tuple[bytes, str]:
        full = self.resolve(rel_path)
        if not full.is_file():
            raise WriteError(f"file not found: {rel_path}")
        if full.is_symlink():
            raise PathRejected(f"symlink rejected: {rel_path}")
        data = full.read_bytes()
        return data, sha256_bytes(data)

    def hash_of(self, rel_path: str) -> Optional[str]:
        full = self.resolve(rel_path)
        if not full.is_file():
            return None
        return sha256_file(full)

    def create(self, rel_path: str, content: str) -> WriteResult:
        """No-clobber create.

        Written to a temp file first and then published with a hard link, so a
        crash can never leave a partially written new file and an existing file
        can never be overwritten.
        """
        data = content.encode("utf-8")
        full = self.resolve(rel_path)
        with self._lock_for(self._nfc(str(full))):
            full.parent.mkdir(parents=True, exist_ok=True)
            if full.exists():
                raise AlreadyExistsError(f"target already exists: {rel_path}")

            tmp = full.with_name(f"{TEMP_PREFIX}{uuid.uuid4().hex}")
            try:
                fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                try:
                    # link() fails if the target appeared meanwhile, so no
                    # pre-existing file can be clobbered.
                    os.link(tmp, full)
                except FileExistsError as exc:
                    raise AlreadyExistsError(
                        f"target already exists: {rel_path}"
                    ) from exc
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

            dir_fd = os.open(full.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

        return WriteResult(
            rel_path=self.relpath(full),
            new_hash=sha256_bytes(data),
            previous_hash=None,
            backup_path=None,
            created=True,
        )

    def save(
        self,
        rel_path: str,
        content: str,
        expected_hash: Optional[str] = None,
        require_existing: bool = True,
    ) -> WriteResult:
        """Optimistic-locked save.

        ``expected_hash`` is mandatory for an existing file. ``None`` is only
        accepted for a genuinely new file, which keeps the "I read this earlier"
        contract honest.
        """
        data = content.encode("utf-8")
        full = self.resolve(rel_path)
        with self._lock_for(self._nfc(str(full))):
            if not full.is_file():
                if require_existing:
                    raise WriteError(f"file not found: {rel_path}")
                return self.create(rel_path, content)

            preimage = full.read_bytes()
            current_hash = sha256_bytes(preimage)

            if expected_hash is None:
                raise ConflictError(
                    "expected_hash is required when overwriting an existing file"
                )
            if current_hash != expected_hash:
                raise ConflictError(
                    "file changed since it was read; reload before saving"
                )

            backup_path = self._backup(full, preimage)

            # Second check immediately before publishing. The in-process lock
            # cannot stop Obsidian from writing in between.
            recheck = sha256_bytes(full.read_bytes())
            if recheck != expected_hash:
                raise ConflictError(
                    "file changed during save; the write was aborted"
                )

            self._write_atomic(full, data)

        return WriteResult(
            rel_path=self.relpath(full),
            new_hash=sha256_bytes(data),
            previous_hash=current_hash,
            backup_path=backup_path,
            created=False,
        )

    def save_json(self, rel_path: str, document: object, expected_hash: Optional[str] = None) -> WriteResult:
        import json

        text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        return self.save(rel_path, text, expected_hash=expected_hash)

    # ------------------------------------------------------------------ misc
    def sweep_temp_files(self, root: Optional[Path] = None) -> int:
        """Delete leftover temp files from a crashed write.

        Temp files are the service's own scratch artifacts, never user content,
        so cleaning them is not a violation of the zero-delete rule.
        """
        base = Path(root).expanduser() if root else self.vault_root
        removed = 0
        for path in base.rglob(f"{TEMP_PREFIX}*"):
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed
