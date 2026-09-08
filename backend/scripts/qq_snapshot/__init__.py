"""Private one-shot QQ snapshot tooling; never imported by workspace runtime."""

from .capture import QQSnapshotError, capture_snapshot

__all__ = ["QQSnapshotError", "capture_snapshot"]
