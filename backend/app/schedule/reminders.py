"""Upcoming Course Reminder Calculation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .storage import ScheduleStorage


def get_upcoming_reminders(
    storage: ScheduleStorage,
    ref_dt: Optional[datetime] = None,
    lookahead_minutes: int = 60,
    semester: str = "2026-2027-1",
) -> List[Dict[str, Any]]:
    """
    Finds classes occurring today whose start time is within `lookahead_minutes`
    (or matching `reminder_minutes` before class).
    Honors temporary overrides (cancelled classes are omitted; relocated rooms are updated).
    """
    now = ref_dt or datetime.now()
    cur_date = now.date()
    week_info = storage.compute_current_week(target_date=cur_date, semester=semester)
    current_week = week_info["current_week"]
    day_of_week = week_info["day_of_week"]

    # If currently not in teaching or exam weeks, no class reminders
    if week_info["phase"] not in ("teaching", "exam"):
        return []

    # Get effective schedule for this week
    effective_slots = storage.get_effective_week_schedule(current_week, semester)
    today_slots = [s for s in effective_slots if s["day_of_week"] == day_of_week]

    reminders = []
    for s in today_slots:
        if s["status"] == "cancelled":
            continue

        start_t_parts = [int(p) for p in s["start_time"].split(":")]
        class_start_dt = datetime.combine(cur_date, datetime.min.time()).replace(
            hour=start_t_parts[0], minute=start_t_parts[1]
        )

        diff_mins = (class_start_dt - now).total_seconds() / 60.0

        # Trigger if starting within reminder_minutes window
        reminder_threshold = s.get("reminder_minutes") or 15
        if 0 <= diff_mins <= max(reminder_threshold, lookahead_minutes):
            reminders.append({
                "course_name": s["course_name"],
                "classroom": s["classroom"],
                "original_classroom": s.get("original_classroom"),
                "teacher": s["teacher"],
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "start_period": s["start_period"],
                "end_period": s["end_period"],
                "minutes_until_start": int(diff_mins),
                "is_relocated": s["status"] == "relocated",
                "is_rescheduled": s["status"] == "rescheduled",
                "meeting_url": s.get("meeting_url"),
                "notes": s.get("notes"),
                "status": s["status"],
                "override_reason": s.get("override_reason"),
            })

    reminders.sort(key=lambda r: r["minutes_until_start"])
    return reminders
