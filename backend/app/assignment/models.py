"""Normalized data models for assignment platform tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

PlatformName = Literal["chaoxing", "smartestu"]
TaskStatus = Literal["unsubmitted", "submitted", "graded", "unknown"]

PLATFORM_LABELS: dict[str, str] = {
    "chaoxing": "学习通",
    "smartestu": "数你最灵",
}


@dataclass
class NormalizedTask:
    """A homework/task item unified across platforms.

    Maps naturally onto schedule.AcademicEvent(event_type="assignment") so the
    schedule reminder banner can consume due-soon tasks without schema glue.
    """

    platform: PlatformName
    external_id: str
    course_name: str
    title: str
    due_at: datetime | None = None
    status: TaskStatus = "unknown"
    score: float | None = None
    detail_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime | None = None

    def validate(self) -> list[str]:
        """Cheap structural validation before persisting; keeps bad adapter
        output out of the DB instead of poisoning the timeline."""
        errors: list[str] = []
        if self.platform not in PLATFORM_LABELS:
            errors.append(f"unknown platform: {self.platform!r}")
        if not self.external_id:
            errors.append("external_id is required")
        if not self.title:
            errors.append("title is required")
        if self.status not in ("unsubmitted", "submitted", "graded", "unknown"):
            errors.append(f"unknown status: {self.status!r}")
        return errors
