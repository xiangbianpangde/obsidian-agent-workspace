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

DEFAULT_LOCK_ROOT = Path.home() / ".personal-ai-workspace" / "papers" / "locks"


def lock_path_for(vault_root: Path) -> Path:
    """Lock file for one specific vault.

    The lock is scoped to the vault it protects, not a single global path. A
    fixed path meant any two instances contended even when they pointed at
    different vaults — and a test using a temporary vault would collide with a
    running server that owns the real one.
    """
    import hashlib

    digest = hashlib.sha256(str(Path(vault_root).resolve()).encode("utf-8")).hexdigest()[:16]
    return DEFAULT_LOCK_ROOT / f"{digest}.lock"


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


def _process_alive(pid: int) -> bool:
    """True when a process with this pid exists.

    Used only to decide whether a lock file is stale. Signalling a live process
    would be unsafe, so the check is deliberately read-only.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user; treat as alive rather than stealing.
        return True
    except OSError:
        return True
    return True


def env_allows_readonly_startup() -> bool:
    """True when this process explicitly opts out of the single-writer lock.

    The lock exists to stop two instances from *writing* one vault. A process
    that only reads — the acceptance tests exercising read-only endpoints, for
    instance — has no reason to contend, and forcing every reader to stop the
    server first makes the subsystem untestable while it runs.

    Opting out is explicit and logged, never automatic: a process that might
    write must still take the lock. Setting this while a writer is active would
    reintroduce the double-write the lock prevents, so it is named for what it
    asserts.
    """
    return os.environ.get("PAPER_ALLOW_READONLY_STARTUP", "").strip() == "1"


class VaultWriteLock:
    """Exclusive advisory lock over one Vault.

    Uses ``O_CREAT | O_EXCL`` on a lock file rather than ``flock`` because the
    failure mode being prevented is a *second process claiming the same Vault*,
    and a stale lock file is far easier to explain than an advisory lock that
    silently stops applying. The PID and start time are written so a confused
    operator can tell who holds it.
    """

    def __init__(self, path: Optional[Path] = None, vault_root: Optional[Path] = None):
        self.vault_root = vault_root
        if path is not None:
            self.path = Path(path).expanduser()
        elif vault_root is not None:
            self.path = lock_path_for(vault_root)
        else:
            raise ValueError("VaultWriteLock needs either a path or a vault_root")
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # A process killed with SIGKILL cannot clean up after itself, so a
            # lock file outliving its holder is the normal outcome of a crash
            # rather than corruption. Requiring the operator to delete it by hand
            # would make every crash a manual intervention, so a lock whose
            # holder is provably gone is reclaimed — and only then.
            holder_pid = self._holder_pid()
            if holder_pid is not None and not _process_alive(holder_pid):
                logger.warning(
                    "reclaiming stale vault lock %s (holder pid %d is gone)",
                    self.path,
                    holder_pid,
                )
                try:
                    self.path.unlink()
                except OSError as exc:
                    raise VaultAlreadyOwnedError(
                        f"stale vault lock at {self.path} could not be removed: {exc}"
                    ) from exc
                return self.acquire()

            holder = self._describe_holder()
            raise VaultAlreadyOwnedError(
                f"another workbench process already writes to this vault "
                f"({self.path}). {holder}Stop that process, then restart."
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

    def _holder_pid(self) -> Optional[int]:
        """PID recorded in the lock file, if it parses."""
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            if line.startswith("pid="):
                try:
                    return int(line[4:].strip())
                except ValueError:
                    return None
        return None

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
