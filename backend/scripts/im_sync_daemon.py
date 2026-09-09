#!/usr/bin/env python3
"""
IM Real-Time Sync Daemon for Personal AI Workspace.

Runs as an independent background helper (similar to wx-cli server).
Monitors local database file modifications for WeCom and QQ:
  - WeCom: WXWork Data/message.db[-wal] -> triggers vault_cli.py decrypt
  - QQ: nt_qq_.../nt_db/nt_msg.db[-wal] -> triggers capture_snapshot("qq_primary")
  - Trigger file: /tmp/im_sync_trigger -> immediate sync on demand

Maintains Sol security boundaries:
  - Operates outside the FastAPI workspace runtime.
  - Decrypted artifacts land in standard local vaults (0700/0600).
  - Workspace adapters ingest snapshots purely through filesystem watermarks.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("im_sync_daemon")

TRIGGER_FILE = Path("/tmp/im_sync_trigger")
DEFAULT_WECOM_DATA_ROOT = (
    Path.home()
    / "Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Data/1688857608826794/Data"
)
DEFAULT_VAULT_CLI = (
    Path.home()
    / "Projects/vendor/yichen-skills/yichen-wecom-local-vault/scripts/vault_cli.py"
)
PYTHON_VENV = Path(__file__).resolve().parents[2] / ".venv/bin/python"


def find_qq_db_dir() -> Optional[Path]:
    qq_root = (
        Path.home()
        / "Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ"
    )
    if not qq_root.is_dir():
        return None
    for pattern in ("nt_qq_*/nt_db", "nt_db"):
        matches = list(qq_root.glob(pattern))
        if matches:
            return matches[0]
    return None


def get_latest_mtime(db_dir: Path, prefix: str) -> float:
    """Returns the maximum modification time among prefix.db, prefix.db-wal, prefix.db-shm."""
    if not db_dir.is_dir():
        return 0.0
    latest = 0.0
    try:
        for p in db_dir.glob(f"{prefix}.db*"):
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                if st.st_mtime > latest:
                    latest = st.st_mtime
    except Exception:
        pass
    return latest


class IMSyncDaemon:
    def __init__(
        self,
        wecom_dir: Optional[Path] = None,
        qq_db_dir: Optional[Path] = None,
        poll_interval: float = 3.0,
        wecom_debounce: float = 3.0,
        qq_debounce: float = 6.0,
    ):
        self.wecom_dir = wecom_dir or DEFAULT_WECOM_DATA_ROOT
        self.qq_db_dir = qq_db_dir or find_qq_db_dir()
        self.poll_interval = poll_interval
        self.wecom_debounce = wecom_debounce
        self.qq_debounce = qq_debounce

        self.last_wecom_mtime = 0.0
        self.last_wecom_sync = 0.0

        self.last_qq_mtime = 0.0
        self.last_qq_sync = 0.0

        self.running = True

    @staticmethod
    def _prune_snapshots(root: Path, keep: int, current_ptr: Optional[Path] = None) -> None:
        """Bound local disk growth of derived snapshot caches.

        Published snapshots are append-only from the ADAPTER's perspective
        (watermarks reference them), but old derived copies carry plaintext
        personal messages — retaining hundreds is a privacy liability, not an
        asset. Only prune directories that are (a) older than the newest
        `keep` entries, (b) fully written (manifest present), and (c) not the
        CURRENT-published target. Disabled entirely when keep <= 0.
        """
        if keep <= 0 or not root.is_dir():
            return
        try:
            current_target: Optional[str] = None
            if current_ptr and current_ptr.is_file():
                current_target = current_ptr.read_text(encoding="utf-8").strip()

            entries = sorted(
                [p for p in root.iterdir() if p.is_dir()],
                key=lambda p: p.name,
            )
            stale = entries[:-keep] if len(entries) > keep else []
            import shutil as _shutil
            for victim in stale:
                # Never touch an incomplete write (no manifest) or CURRENT target
                if not (victim / "manifest.json").is_file():
                    continue
                if current_target and victim.name == current_target:
                    continue
                try:
                    _shutil.rmtree(victim)
                    logger.info("Pruned old snapshot %s", victim.name)
                except Exception as exc:
                    logger.warning("Could not prune %s: %s", victim.name, exc)
        except Exception as exc:
            logger.warning("Prune check failed: %s", exc)

    def _prune_all(self) -> None:
        keep = int(os.environ.get("IM_SNAPSHOT_KEEP", "24"))
        snap_root = Path.home() / "Library/Application Support/wecom-local-vault/snapshots"
        self._prune_snapshots(snap_root, keep)
        qq_acct = Path.home() / "Library/Application Support/qq-local-vault/accounts/qq_primary"
        self._prune_snapshots(qq_acct / "snapshots", keep, current_ptr=qq_acct / "CURRENT")

    def sync_wecom(self, reason: str = "change") -> bool:
        if not self.wecom_dir.is_dir():
            return False
        if not DEFAULT_VAULT_CLI.is_file():
            logger.warning("WeCom vault_cli.py not found at %s", DEFAULT_VAULT_CLI)
            return False

        logger.info("Syncing WeCom (reason: %s)...", reason)
        t0 = time.time()
        try:
            cmd = [
                str(PYTHON_VENV if PYTHON_VENV.is_file() else sys.executable),
                str(DEFAULT_VAULT_CLI),
                "decrypt",
                "--data-dir",
                str(self.wecom_dir),
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            elapsed = time.time() - t0
            if proc.returncode == 0:
                logger.info("WeCom sync succeeded in %.2fs", elapsed)
                self.last_wecom_sync = time.time()
                return True
            else:
                logger.warning("WeCom sync failed (rc=%d): %s", proc.returncode, proc.stderr[:200])
                return False
        except Exception as exc:
            logger.error("WeCom sync exception: %s", exc)
            return False

    def sync_qq(self, reason: str = "change") -> bool:
        # Check if QQ process is alive
        try:
            pgrep = subprocess.run(["pgrep", "-x", "QQ"], capture_output=True)
            if pgrep.returncode != 0:
                logger.debug("QQ not running, skipping snapshot")
                return False
        except Exception:
            pass

        logger.info("Syncing QQ (reason: %s)...", reason)
        t0 = time.time()
        try:
            # Import capture_snapshot directly in the daemon process
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from backend.scripts.qq_snapshot.capture import capture_snapshot

            res = capture_snapshot(account_alias="qq_primary")
            elapsed = time.time() - t0
            snap_id = res.get("snapshot_id", "unknown")
            msg_count = res.get("message_count", 0)
            logger.info("QQ sync succeeded in %.2fs: %s (%d messages)", elapsed, snap_id, msg_count)
            self.last_qq_sync = time.time()
            self._prune_all()
            return True
        except Exception as exc:
            logger.warning("QQ sync skipped/failed: %s", exc)
            return False

    def run(self) -> None:
        logger.info("Starting IM Sync Daemon (poll_interval=%.1fs)...", self.poll_interval)
        logger.info("WeCom dir: %s", self.wecom_dir)
        logger.info("QQ DB dir: %s", self.qq_db_dir)

        # Initial baseline capture
        self.last_wecom_mtime = get_latest_mtime(self.wecom_dir, "message")
        if self.qq_db_dir:
            self.last_qq_mtime = get_latest_mtime(self.qq_db_dir, "nt_msg")

        # Perform initial sync on startup to ensure freshness
        self.sync_wecom(reason="startup")
        self.sync_qq(reason="startup")

        while self.running:
            try:
                now = time.time()

                # 1. Check on-demand trigger file
                if TRIGGER_FILE.exists():
                    try:
                        TRIGGER_FILE.unlink(missing_ok=True)
                    except Exception:
                        pass
                    logger.info("Manual trigger received via /tmp/im_sync_trigger")
                    self.sync_wecom(reason="manual_trigger")
                    self.sync_qq(reason="manual_trigger")

                # 2. Check WeCom file modifications
                wecom_mtime = get_latest_mtime(self.wecom_dir, "message")
                if wecom_mtime > self.last_wecom_mtime:
                    if now - self.last_wecom_sync >= self.wecom_debounce:
                        self.last_wecom_mtime = wecom_mtime
                        self.sync_wecom(reason=f"mtime_change (+{wecom_mtime - self.last_wecom_mtime:.1f}s)")

                # 3. Check QQ file modifications
                if self.qq_db_dir:
                    qq_mtime = get_latest_mtime(self.qq_db_dir, "nt_msg")
                    if qq_mtime > self.last_qq_mtime:
                        if now - self.last_qq_sync >= self.qq_debounce:
                            self.last_qq_mtime = qq_mtime
                            self.sync_qq(reason=f"mtime_change (+{qq_mtime - self.last_qq_mtime:.1f}s)")

                time.sleep(self.poll_interval)
            except (KeyboardInterrupt, SystemExit):
                break
            except Exception as exc:
                logger.error("Daemon loop error: %s", exc)
                time.sleep(self.poll_interval)

        logger.info("IM Sync Daemon stopped cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description="IM Sync Daemon for WeCom and QQ")
    parser.add_argument("--interval", type=float, default=3.0, help="Poll interval in seconds")
    parser.add_argument("--wecom-debounce", type=float, default=3.0, help="WeCom debounce in seconds")
    parser.add_argument("--qq-debounce", type=float, default=6.0, help="QQ debounce in seconds")
    args = parser.parse_args()

    daemon = IMSyncDaemon(
        poll_interval=args.interval,
        wecom_debounce=args.wecom_debounce,
        qq_debounce=args.qq_debounce,
    )

    def sig_handler(sig, frame):
        daemon.running = False

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    daemon.run()


if __name__ == "__main__":
    main()
