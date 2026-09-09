"""One-shot, fail-closed QQ NT snapshot extractor for macOS.

The workspace runtime never imports this module. It briefly freezes every
process holding the source nt_db open, clones the encrypted DB/WAL/SHM set,
resumes QQ, decrypts only frozen copies, validates them, and atomically
publishes an append-only local snapshot.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .profiles import (
    CODEC_PROFILES,
    CRITICAL_SCHEMA,
    LOCATOR_PROFILE_ID,
    NORMALIZATION_PROFILE_ID,
    REQUIRED_EXPORTS,
    SCHEMA_PROFILE_ID,
    critical_schema_fingerprint,
)

ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SNAPSHOT_RE = re.compile(r"^qqsnap-v1-[0-9a-f]{24}$")
SOURCE_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:\.db(?:-(?:wal|shm))?|\.material)$")
CUSTOM_HEADER_SIZE = 1024
DEFAULT_VAULT_ROOT = Path.home() / "Library/Application Support/qq-local-vault"
SAFE_ERROR_CODES = {
    "QQ_SNAPSHOT_PERMISSION",
    "QQ_SNAPSHOT_PATH_REJECTED",
    "QQ_SNAPSHOT_CODEC_UNSUPPORTED",
    "QQ_SNAPSHOT_CODEC_NOT_LIVE",
    "QQ_SNAPSHOT_CODEC_AMBIGUOUS",
    "QQ_SNAPSHOT_KEY_MISMATCH",
    "QQ_SNAPSHOT_INTEGRITY_FAILED",
    "QQ_SNAPSHOT_SCHEMA_UNSUPPORTED",
    "QQ_SNAPSHOT_IDENTITY_CONFLICT",
    "QQ_SNAPSHOT_SOURCE_MISSING",
    "QQ_SNAPSHOT_SOURCE_HEADER",
    "QQ_SNAPSHOT_SOURCE_SALT",
    "QQ_SNAPSHOT_WRITER_UNSAFE",
    "QQ_SNAPSHOT_RESUME_FAILED",
    "QQ_SNAPSHOT_EXPORT_FAILED",
    "QQ_SNAPSHOT_PUBLISH_CONFLICT",
    "QQ_SNAPSHOT_CAPTURE_FAILED",
    "QQ_SNAPSHOT_WRAPPER_MISSING",
    "QQ_SNAPSHOT_TOOL_MISSING",
}


class QQSnapshotError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in SAFE_ERROR_CODES else "QQ_SNAPSHOT_CAPTURE_FAILED"
        super().__init__(self.code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_private_dir(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    st = path.stat()
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise QQSnapshotError("QQ_SNAPSHOT_PERMISSION")


def _validate_private_regular(path: Path, *, mode: int = 0o600) -> None:
    st = path.lstat()
    if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid():
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
    if st.st_nlink != 1 or (st.st_mode & 0o077):
        raise QQSnapshotError("QQ_SNAPSHOT_PERMISSION")
    if (st.st_mode & 0o777) != mode:
        raise QQSnapshotError("QQ_SNAPSHOT_PERMISSION")


def _auto_detect_db_root() -> Path:
    base = Path.home() / "Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ"
    candidates = []
    if base.is_dir():
        for account in base.glob("nt_qq_*/nt_db"):
            if (account / "nt_msg.db").is_file():
                candidates.append(account)
    if len(candidates) != 1:
        raise QQSnapshotError("QQ_SNAPSHOT_SOURCE_MISSING")
    return candidates[0]


def _validate_source_root(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
    expected = Path.home() / "Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ"
    try:
        path.relative_to(expected)
    except ValueError as exc:
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED") from exc
    for name in REQUIRED_EXPORTS:
        candidate = path / name
        if candidate.is_symlink() or not candidate.is_file():
            raise QQSnapshotError("QQ_SNAPSHOT_SOURCE_MISSING")
        with candidate.open("rb") as handle:
            header = handle.read(64)
        if not header.startswith(b"SQLite header 3\x00") or b"QQ_NT DB" not in header:
            raise QQSnapshotError("QQ_SNAPSHOT_SOURCE_HEADER")
    return path


def _read_current(account_root: Path) -> str | None:
    path = account_root / "CURRENT"
    if not path.exists():
        return None
    _validate_private_regular(path)
    value = path.read_text(encoding="ascii").strip()
    if not SNAPSHOT_RE.fullmatch(value):
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
    return value


@contextmanager
def _publish_lock(account_root: Path) -> Iterator[None]:
    lock_path = account_root / "publish.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _writer_pids(source_root: Path) -> list[int]:
    lsof = shutil.which("lsof")
    if not lsof:
        raise QQSnapshotError("QQ_SNAPSHOT_TOOL_MISSING")
    proc = subprocess.run(
        [lsof, "-t", "+D", os.fspath(source_root)],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    pids = sorted({int(line) for line in proc.stdout.splitlines() if line.strip().isdigit()})
    if not pids:
        raise QQSnapshotError("QQ_SNAPSHOT_WRITER_UNSAFE")
    for pid in pids:
        identity = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        if "/QQ.app/Contents/" not in identity and "/QQUpdate.app/Contents/" not in identity:
            raise QQSnapshotError("QQ_SNAPSHOT_WRITER_UNSAFE")
    return pids


def _process_token(pid: int) -> str:
    proc = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise QQSnapshotError("QQ_SNAPSHOT_WRITER_UNSAFE")
    return hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()


def _process_is_stopped(pid: int) -> bool:
    proc = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return "T" in proc.stdout.strip()


class _ResumeGuard:
    def __init__(self, pids: list[int], timeout_secs: float = 30.0):
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            try:
                os.close(write_fd)
                ready, _, _ = select.select([read_fd], [], [], timeout_secs)
                signal_byte = os.read(read_fd, 1) if ready else b""
                if signal_byte != b"D":
                    for pid in pids:
                        try:
                            os.kill(pid, signal.SIGCONT)
                        except ProcessLookupError:
                            pass
            finally:
                os._exit(0)
        os.close(read_fd)
        self._write_fd = write_fd
        self._child = child

    def disarm(self) -> None:
        if self._write_fd is None:
            return
        try:
            os.write(self._write_fd, b"D")
        finally:
            os.close(self._write_fd)
            self._write_fd = None
            os.waitpid(self._child, 0)


def _loaded_wrapper_path(pid: int) -> Path:
    lsof = shutil.which("lsof")
    if not lsof:
        raise QQSnapshotError("QQ_SNAPSHOT_TOOL_MISSING")
    proc = subprocess.run(
        [lsof, "-a", "-p", str(pid), "-Fn"],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("n") and line.endswith("/wrapper.node"):
            path = Path(line[1:])
            if path.is_file() and not path.is_symlink():
                return path
    raise QQSnapshotError("QQ_SNAPSHOT_WRAPPER_MISSING")


def _capture_live_keys(pid: int, source_root: Path, wrapper_digest: str) -> dict[str, Any]:
    lldb = shutil.which("lldb")
    if not lldb:
        raise QQSnapshotError("QQ_SNAPSHOT_TOOL_MISSING")
    module_path = Path(__file__).with_name("lldb_bridge.py")
    read_fd, write_fd = os.pipe()
    env = os.environ.copy()
    env["QQ_SNAPSHOT_SECRET_FD"] = str(write_fd)
    env["QQ_SNAPSHOT_DB_ROOT"] = os.fspath(source_root)
    env["QQ_SNAPSHOT_WRAPPER_SHA256"] = wrapper_digest
    command = [
        lldb,
        "-p",
        str(pid),
        "-o",
        f"command script import {module_path}",
        "-o",
        "qq_secure_capture",
        "-o",
        "process detach",
        "-o",
        "quit",
    ]
    try:
        proc = subprocess.Popen(
            command,
            env=env,
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(write_fd)
        write_fd = -1
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            raise QQSnapshotError("QQ_SNAPSHOT_CAPTURE_FAILED")
        chunks = []
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        if proc.returncode != 0 or not chunks:
            raise QQSnapshotError("QQ_SNAPSHOT_CAPTURE_FAILED")
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if payload.get("error_code"):
            raise QQSnapshotError(payload["error_code"])
        wrapper_digest = payload.get("wrapper_sha256")
        if wrapper_digest not in CODEC_PROFILES:
            raise QQSnapshotError("QQ_SNAPSHOT_CODEC_UNSUPPORTED")
        if set(payload.get("keys", {})) != set(REQUIRED_EXPORTS):
            raise QQSnapshotError("QQ_SNAPSHOT_KEY_MISMATCH")
        return payload
    except (OSError, json.JSONDecodeError) as exc:
        raise QQSnapshotError("QQ_SNAPSHOT_CAPTURE_FAILED") from exc
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)


def _clonefile(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    clonefile = libc.clonefile
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    result = clonefile(os.fsencode(source), os.fsencode(target), 0)
    if result != 0:
        raise QQSnapshotError("QQ_SNAPSHOT_CAPTURE_FAILED")
    os.chmod(target, 0o600)
    st = target.lstat()
    if st.st_nlink != 1 or st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise QQSnapshotError("QQ_SNAPSHOT_PERMISSION")


def _source_files(source_root: Path) -> list[Path]:
    files = []
    for path in source_root.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if SOURCE_FILE_RE.fullmatch(path.name):
            files.append(path)
    if not all((source_root / name) in files for name in REQUIRED_EXPORTS):
        raise QQSnapshotError("QQ_SNAPSHOT_SOURCE_MISSING")
    return sorted(files, key=lambda item: item.name)


def _freeze_clone(source_root: Path, target: Path, pids: list[int]) -> str:
    tokens = {pid: _process_token(pid) for pid in pids}
    owned = [pid for pid in pids if not _process_is_stopped(pid)]
    guard = _ResumeGuard(owned)
    resumed = False
    try:
        for pid in owned:
            if _process_token(pid) != tokens[pid]:
                raise QQSnapshotError("QQ_SNAPSHOT_WRITER_UNSAFE")
            os.kill(pid, signal.SIGSTOP)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(_process_is_stopped(pid) for pid in owned):
            time.sleep(0.05)
        if not all(_process_is_stopped(pid) for pid in owned):
            raise QQSnapshotError("QQ_SNAPSHOT_WRITER_UNSAFE")

        quiesced_at = _utc_now()
        files = _source_files(source_root)
        before = {p.name: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) for p in files}
        if source_root.stat().st_dev != target.parent.stat().st_dev:
            raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
        _ensure_private_dir(target)
        for source in files:
            _clonefile(source, target / source.name)
        after = {p.name: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) for p in files}
        if before != after:
            raise QQSnapshotError("QQ_SNAPSHOT_INTEGRITY_FAILED")

        for pid in owned:
            if _process_token(pid) != tokens[pid]:
                raise QQSnapshotError("QQ_SNAPSHOT_RESUME_FAILED")
            os.kill(pid, signal.SIGCONT)
        resumed = True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_process_is_stopped(pid) for pid in owned):
            time.sleep(0.05)
        if any(_process_is_stopped(pid) for pid in owned):
            raise QQSnapshotError("QQ_SNAPSHOT_RESUME_FAILED")
        return quiesced_at
    finally:
        if not resumed:
            for pid in owned:
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        guard.disarm()


def _sqlcipher_export(raw: Path, output: Path, key_hex: str, salt_hex: str, profile) -> None:
    sqlcipher = shutil.which("sqlcipher")
    if not sqlcipher:
        raise QQSnapshotError("QQ_SNAPSHOT_TOOL_MISSING")
    work_dir = raw.parent.parent / "work"
    _ensure_private_dir(work_dir)
    clean = work_dir / raw.name
    with raw.open("rb") as source:
        header = source.read(64)
        if not header.startswith(b"SQLite header 3\x00") or b"QQ_NT DB" not in header:
            raise QQSnapshotError("QQ_SNAPSHOT_SOURCE_HEADER")
        source.seek(CUSTOM_HEADER_SIZE)
        with clean.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
    os.chmod(clean, 0o600)
    for suffix in ("-wal",):
        sidecar = raw.with_name(raw.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, clean.with_name(clean.name + suffix))
            os.chmod(clean.with_name(clean.name + suffix), 0o600)

    keyspec = "x'" + key_hex + salt_hex + "'"
    safe_key = keyspec.replace("'", "''")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    sql = "\n".join(
        [
            f"PRAGMA key = '{safe_key}';",
            f"PRAGMA cipher_page_size = {profile.page_size};",
            f"PRAGMA kdf_iter = {profile.kdf_iter};",
            f"PRAGMA cipher_hmac_algorithm = {profile.hmac_algorithm};",
            f"PRAGMA cipher_default_kdf_algorithm = {profile.kdf_algorithm};",
            f"ATTACH DATABASE '{str(output).replace(chr(39), chr(39) * 2)}' AS plaintext KEY '';",
            "SELECT sqlcipher_export('plaintext');",
            "DETACH DATABASE plaintext;",
            ".quit",
            "",
        ]
    )
    proc = subprocess.run(
        [sqlcipher, os.fspath(clean)],
        input=sql,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
        check=False,
    )
    # Best-effort removal of Python references to secret text.
    keyspec = ""
    safe_key = ""
    sql = ""
    if proc.returncode != 0 or not output.is_file():
        raise QQSnapshotError("QQ_SNAPSHOT_EXPORT_FAILED")
    os.chmod(output, 0o600)


def _validate_schema(path: Path, expected: dict[str, dict[str, tuple[str, int]]]) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_nlink != 1:
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise QQSnapshotError("QQ_SNAPSHOT_INTEGRITY_FAILED")
    uri = f"file:{path}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        for table, required_columns in expected.items():
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            actual = {str(row[1]): (str(row[2]).upper(), int(row[5])) for row in rows}
            for name, specification in required_columns.items():
                if actual.get(name) != specification:
                    raise QQSnapshotError("QQ_SNAPSHOT_SCHEMA_UNSUPPORTED")
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise QQSnapshotError("QQ_SNAPSHOT_INTEGRITY_FAILED")
            integrity_scope = "full"
        except sqlite3.OperationalError:
            # Some QQ databases retain FTS tables that require Tencent's private
            # pinyin tokenizer. Stock SQLite cannot instantiate those virtual
            # tables, so validate every critical ordinary table individually.
            for table in expected:
                row = connection.execute(f'PRAGMA integrity_check("{table}")').fetchone()
                if not row or row[0] != "ok":
                    raise QQSnapshotError("QQ_SNAPSHOT_INTEGRITY_FAILED")
            integrity_scope = "critical_tables"
        return {"integrity": integrity_scope, "size": path.stat().st_size, "sha256": _sha256(path)}
    except sqlite3.DatabaseError as exc:
        raise QQSnapshotError("QQ_SNAPSHOT_INTEGRITY_FAILED") from exc
    finally:
        connection.close()


def _message_stats(path: Path) -> dict[str, Any]:
    uri = f"file:{path}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        result: dict[str, Any] = {"tables": {}, "earliest_epoch": None, "latest_epoch": None}
        times = []
        for table in ("group_msg_table", "c2c_msg_table"):
            row = connection.execute(
                f'SELECT COUNT(*), COUNT(DISTINCT "40001"), MIN("40050"), MAX("40050") FROM "{table}"'
            ).fetchone()
            count, unique_count, earliest, latest = (int(row[0]), int(row[1]), row[2], row[3])
            if count != unique_count:
                raise QQSnapshotError("QQ_SNAPSHOT_IDENTITY_CONFLICT")
            result["tables"][table] = {"count": count, "unique_locator_count": unique_count}
            if earliest:
                times.append(int(earliest))
            if latest:
                times.append(int(latest))
        if times:
            result["earliest_epoch"] = min(times)
            result["latest_epoch"] = max(times)
        return result
    finally:
        connection.close()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    os.chmod(path, 0o600)
    _fsync_dir(path.parent)


def _publish_current(account_root: Path, snapshot_id: str, expected_parent: str | None) -> None:
    if _read_current(account_root) != expected_parent:
        raise QQSnapshotError("QQ_SNAPSHOT_PUBLISH_CONFLICT")
    temp = account_root / (".CURRENT." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (snapshot_id + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, account_root / "CURRENT")
    os.chmod(account_root / "CURRENT", 0o600)
    _fsync_dir(account_root)


def _quarantine(run_dir: Path, quarantine_root: Path, code: str) -> None:
    if not run_dir.exists():
        return
    _ensure_private_dir(quarantine_root)
    target = quarantine_root / f"{run_dir.name}-{code.lower()}"
    if target.exists():
        target = quarantine_root / f"{run_dir.name}-{uuid.uuid4().hex[:8]}"
    os.replace(run_dir, target)
    _fsync_dir(quarantine_root)


def capture_snapshot(account_alias: str, source_root: Path | None = None, vault_root: Path | None = None) -> dict[str, Any]:
    if not ACCOUNT_RE.fullmatch(account_alias):
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
    os.umask(0o077)
    source_root = _validate_source_root(source_root or _auto_detect_db_root())
    vault_root = (vault_root or DEFAULT_VAULT_ROOT).expanduser()
    if not vault_root.is_absolute() or vault_root.is_symlink():
        raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")

    account_root = vault_root / "accounts" / account_alias
    for directory in (
        vault_root,
        vault_root / "accounts",
        account_root,
        account_root / "private",
        account_root / "staging",
        account_root / "quarantine",
        account_root / "snapshots",
    ):
        _ensure_private_dir(directory)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    run_dir = account_root / "staging" / run_id
    _ensure_private_dir(run_dir)
    source_clone = run_dir / "source"
    export_dir = run_dir / "export"

    try:
        with _publish_lock(account_root):
            expected_parent = _read_current(account_root)
            pids = _writer_pids(source_root)
            wrapper = _loaded_wrapper_path(pids[0])
            wrapper_digest = _sha256(wrapper)
            profile = CODEC_PROFILES.get(wrapper_digest)
            if profile is None:
                raise QQSnapshotError("QQ_SNAPSHOT_CODEC_UNSUPPORTED")

            # Check private/keys.json cache (0600) to avoid slow LLDB memory scan on repeated captures
            key_cache = account_root / "private" / "keys.json"
            key_payload = None
            if key_cache.is_file():
                try:
                    _validate_private_regular(key_cache)
                    cached = json.loads(key_cache.read_text(encoding="utf-8"))
                    if cached.get("wrapper_sha256") == wrapper_digest and cached.get("profile_id") == profile.profile_id:
                        key_payload = cached
                except Exception:
                    key_payload = None

            if key_payload is None:
                key_payload = _capture_live_keys(pids[0], source_root, wrapper_digest)
                if key_payload.get("profile_id") != profile.profile_id:
                    raise QQSnapshotError("QQ_SNAPSHOT_CODEC_UNSUPPORTED")
                # Persist to private/keys.json with 0600 permissions
                try:
                    _atomic_json(key_cache, key_payload)
                except Exception:
                    pass

            source_quiesced_at = _freeze_clone(source_root, source_clone, pids)
            _ensure_private_dir(export_dir)
            for name in REQUIRED_EXPORTS:
                material = key_payload["keys"].get(name) or {}
                key_hex = material.get("key_hex", "")
                salt_hex = material.get("salt_hex", "")
                if len(key_hex) != 64 or len(salt_hex) != 32:
                    raise QQSnapshotError("QQ_SNAPSHOT_KEY_MISMATCH")
                _sqlcipher_export(source_clone / name, export_dir / name, key_hex, salt_hex, profile)
                material["key_hex"] = ""
                material["salt_hex"] = ""
            key_payload["keys"] = {}

            validations = {}
            for name in REQUIRED_EXPORTS:
                validations[name] = _validate_schema(export_dir / name, CRITICAL_SCHEMA[name])
            stats = _message_stats(export_dir / "nt_msg.db")

            work_dir = run_dir / "work"
            if work_dir.exists():
                marker = work_dir.parent == run_dir and work_dir.name == "work"
                if not marker:
                    raise QQSnapshotError("QQ_SNAPSHOT_PATH_REJECTED")
                shutil.rmtree(work_dir)

            raw_files = []
            for path in sorted(source_clone.iterdir(), key=lambda item: item.name):
                if path.is_file() and not path.is_symlink():
                    raw_files.append({"name": path.name, "size": path.stat().st_size, "sha256": _sha256(path)})
            export_files = [
                {
                    "role": name.removesuffix(".db"),
                    "path": f"export/{name}",
                    "size": validations[name]["size"],
                    "sha256": validations[name]["sha256"],
                }
                for name in REQUIRED_EXPORTS
            ]
            version_match = re.search(r"/versions/([^/]+)/", os.fspath(wrapper))
            core = {
                "schema": "qq.snapshot/v1",
                "account_alias": account_alias,
                "parent_snapshot_id": expected_parent,
                "source": {
                    "app_version": version_match.group(1) if version_match else "unknown",
                    "wrapper_sha256": wrapper_digest,
                    "source_quiesced_at": source_quiesced_at,
                },
                "codec_profile_id": profile.profile_id,
                "schema_profile_id": SCHEMA_PROFILE_ID,
                "critical_schema_fingerprint": critical_schema_fingerprint(),
                "locator_profile_id": LOCATOR_PROFILE_ID,
                "normalization_profile_id": NORMALIZATION_PROFILE_ID,
                "coverage": {
                    "from_epoch": stats["earliest_epoch"],
                    "through_epoch": stats["latest_epoch"],
                    "source_through_at": source_quiesced_at,
                    "gaps": [
                        "unsupported_message_classes:guild,temp,discuss,service_assistant",
                        "structured_mentions_unverified",
                        "reply_locator_unverified",
                        "recall_events_unverified",
                    ],
                },
                "stats": stats,
                "raw_files": raw_files,
                "files": export_files,
                "created_at": _utc_now(),
            }
            canonical = json.dumps(core, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
            snapshot_id = "qqsnap-v1-" + hashlib.sha256(canonical).hexdigest()[:24]
            manifest = {**core, "snapshot_id": snapshot_id}
            _atomic_json(run_dir / "manifest.json", manifest)

            for path in run_dir.rglob("*"):
                if path.is_file():
                    os.chmod(path, 0o600)
                    _fsync_file(path)
                elif path.is_dir():
                    os.chmod(path, 0o700)
                    _fsync_dir(path)
            _fsync_dir(run_dir)

            published = account_root / "snapshots" / snapshot_id
            if published.exists():
                existing = published / "manifest.json"
                if not existing.is_file() or json.loads(existing.read_text(encoding="utf-8")) != manifest:
                    raise QQSnapshotError("QQ_SNAPSHOT_PUBLISH_CONFLICT")
                _quarantine(run_dir, account_root / "quarantine", "duplicate")
            else:
                os.replace(run_dir, published)
                _fsync_dir(published.parent)
            _publish_current(account_root, snapshot_id, expected_parent)
            return {
                "snapshot_id": snapshot_id,
                "message_count": sum(item["count"] for item in stats["tables"].values()),
                "source_quiesced_at": source_quiesced_at,
            }
    except QQSnapshotError as exc:
        _quarantine(run_dir, account_root / "quarantine", exc.code)
        raise
    except Exception as exc:
        _quarantine(run_dir, account_root / "quarantine", "capture-failed")
        raise QQSnapshotError("QQ_SNAPSHOT_CAPTURE_FAILED") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a private, read-only QQ NT snapshot")
    parser.add_argument("--confirm-capture", action="store_true", help="confirm authorized local key capture")
    parser.add_argument("--account-alias", default=os.environ.get("QQ_ACCOUNT_ID", "qq_primary"))
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--vault-root", type=Path)
    args = parser.parse_args(argv)
    if not args.confirm_capture:
        print("QQ_SNAPSHOT_CONFIRMATION_REQUIRED", file=sys.stderr)
        return 2
    try:
        result = capture_snapshot(args.account_alias, args.source_root, args.vault_root)
    except QQSnapshotError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    print(f"QQ_SNAPSHOT_OK {result['snapshot_id']} messages={result['message_count']}")
    return 0
