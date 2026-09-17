"""Single-writer guard for the paper subsystem.

ADR-007 relies on in-process locks: ``VaultWriteService`` serialises per path and
``_mutate_sidecar`` serialises per paper. Those guarantees only hold while one
process owns the Vault. Two independent single-worker instances, or one instance
started with ``--workers 4``, each get their own lock tables and would happily
write over each other.

Sol's ruling was explicit that a startup assertion is necessary but is *not* a
substitute for the concurrency model: the assertion stops an obviously wrong
launch, and the lockfile stops two separately-started processes from claiming
the same Vault.

Neither mechanism is a substitute for the in-process locks — they are the outer
two layers of three.
"""

from __future__ import annotations

import atexit
import errno
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_LOCK_PATH = Path.home() / ".personal-ai-workspace" / "papers" / "vault.lock"


class MultiWorkerError(RuntimeError):
    """The process was started with more than one worker."""


class VaultAlreadyOwnedError(RuntimeError):
    """Another live process already holds the write lock for this Vault."""


def assert_single_worker(workers: int | None = None) -> None:
    """Refuse to start with more than one worker.

    Multiple workers would each hold their own path locks, so two requests could
    interleave a read-modify-write on the same sidecar and one would lose. The
    check is deliberately loud: silently allowing it would reintroduce the exact
    data-loss class the review found.
    """
    if workers is None:
        workers = int(os.environ.get("WEB_CONCURRENCY", "1") or "1")
    if workers > 1:
        raise MultiWorkerError(
            f"the paper subsystem requires a single worker (got {workers}). "
            "Its write coordination is in-process; multiple workers would "
            "silently lose concurrent updates. Start with --workers 1."
        )


class VaultWriteLock:
    """Exclusive advisory lock over one Vault.

    Uses ``O_CREAT | O_EXCL`` on a lock file rather than ``flock`` because the
    failure mode being prevented is a *second process claiming the same Vault*,
    and a stale lock file is far easier to explain than an advisory lock that
    silently stops applying. The PID and start time are written so a confused
    operator can tell who holds it.
    """

    def __init__(self, path: Optional[Path] = None, vault_root: Optional[Path] = None):
        self.path = Path(path).expanduser() if path else DEFAULT_LOCK_PATH
        self.vault_root = vault_root
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = self._describe_holder()
            raise VaultAlreadyOwnedError(
                f"another workbench process already writes to this vault "
                f"({self.path}). {holder}If that process is gone, delete the "
                "lock file and restart."
            ) from None
        except OSError as exc:  # pragma: no cover - filesystem specific
            if exc.errno == errno.EACCES:
                raise VaultAlreadyOwnedError(
                    f"cannot acquire the vault lock at {self.path}: permission denied"
                ) from exc
            raise

        try:
            os.write(
                fd,
                (
                    f"pid={os.getpid()}\n"
                    f"vault={self.vault_root or 'unknown'}\n"
                ).encode("utf-8"),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        self._acquired = True
        atexit.register(self.release)

    def _describe_holder(self) -> str:
        try:
            content = self.path.read_text(encoding="utf-8").strip().replace("\n", ", ")
        except OSError:
            return ""
        return f"Holder: {content}. " if content else ""

    def release(self) -> None:
        """Release the lock. Never deletes anything else."""
        if not self._acquired:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove vault lock file: %s", self.path)
        self._acquired = False

    def __enter__(self) -> "VaultWriteLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
