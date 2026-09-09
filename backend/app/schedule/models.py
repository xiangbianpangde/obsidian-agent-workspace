"""Schedule & Academic Calendar Data Models (v0.2.8 / R6-R10).

Implements:
- Slot-level occurrence binding for CourseOverride (time_slot_id)
- Zero Delete compliance via soft delete (is_deleted, is_revoked)
- Consistent period-to-time derivation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional


@dataclass
class CourseTimeSlot:
    """Represents a scheduled period slot for a course."""

    id: str
    course_id: str
    day_of_week: int  # 1 = Monday ... 7 = Sunday
    start_period: int  # 1 .. 11
    end_period: int  # 1 .. 11
    start_time: str  # "08:00"
    end_time: str  # "09:40"
    week_pattern: Literal["all", "odd", "even", "custom"] = "all"
    start_week: int = 1
    end_week: int = 16
    custom_weeks: List[int] = field(default_factory=list)
    classroom: str = ""
    is_deleted: bool = False


@dataclass
class Course:
    """A university course in a specific semester."""

    id: str
    name: str
    code: str = ""
    teacher: str = ""
    classroom: str = ""
    credits: float = 2.0
    semester: str = "2026-2027-1"
    color: str = "#3b82f6"  # Hex color for UI
    notes: str = ""
    course_group_id: Optional[str] = None  # Linked WeCom/QQ channel ID
    meeting_url: Optional[str] = None  # Tencent Meeting or Zoom link
    reminder_minutes: int = 15  # Default 15 mins before class
    time_slots: List[CourseTimeSlot] = field(default_factory=list)
    is_deleted: bool = False
    deleted_at: Optional[str] = None


@dataclass
class CourseOverride:
    """
    Occurrence-level temporary change.
    Explicitly binds to `time_slot_id` so other slots on the same day are unaffected.
    Does NOT modify the base semester schedule.
    Zero Delete: revoked overrides are preserved with is_revoked=True.
    """

    id: str
    time_slot_id: str  # Precise slot occurrence binding
    course_id: str
    semester: str
    week_number: int
    day_of_week: int
    override_type: Literal["cancel", "reschedule", "relocate", "makeup"]
    new_classroom: Optional[str] = None
    new_day_of_week: Optional[int] = None
    new_start_period: Optional[int] = None
    new_end_period: Optional[int] = None
    new_start_time: Optional[str] = None
    new_end_time: Optional[str] = None
    reason: str = ""
    is_revoked: bool = False
    revoked_at: Optional[str] = None


@dataclass
class HolidayRule:
    name: str
    start_date: str  # "YYYY-MM-DD"
    end_date: str  # "YYYY-MM-DD"
    is_off: bool = True
    makeup_days: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class AcademicCalendar:
    semester: str = "2026-2027-1"
    start_date: str = "2026-08-31"  # Fall 2026 Monday start date
    total_weeks: int = 20
    teaching_weeks_start: int = 1
    teaching_weeks_end: int = 16
    exam_weeks_start: int = 17
    exam_weeks_end: int = 18
    holidays: List[HolidayRule] = field(default_factory=list)


@dataclass
class AcademicEvent:
    """Integrated academic and personal events linked with the schedule."""

    id: str
    semester: str
    title: str
    event_type: Literal["assignment", "exam", "lab", "meeting", "activity", "other"]
    due_date: str  # "YYYY-MM-DD"
    due_time: Optional[str] = "23:59"
    week_number: Optional[int] = None
    related_course_id: Optional[str] = None
    location: str = ""
    notes: str = ""
    is_completed: bool = False
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    is_deleted: bool = False
    deleted_at: Optional[str] = None


# Standard Period Times (Central South University for Nationalities / 中南民族大学通用作息时间)
STANDARD_PERIODS = [
    {"period": 1, "start": "08:00", "end": "08:45"},
    {"period": 2, "start": "08:55", "end": "09:40"},
    {"period": 3, "start": "10:00", "end": "10:45"},
    {"period": 4, "start": "10:55", "end": "11:40"},
    {"period": 5, "start": "14:10", "end": "14:55"},
    {"period": 6, "start": "15:05", "end": "15:50"},
    {"period": 7, "start": "16:00", "end": "16:45"},
    {"period": 8, "start": "16:55", "end": "17:40"},
    {"period": 9, "start": "18:40", "end": "19:25"},
    {"period": 10, "start": "19:30", "end": "20:15"},
    {"period": 11, "start": "20:20", "end": "21:05"},
]


def period_range_to_time(start_p: int, end_p: int) -> tuple[str, str]:
    """Derives exact start and end times from start and end period numbers."""
    start_time = "08:00"
    end_time = "09:40"
    for p in STANDARD_PERIODS:
        if p["period"] == start_p:
            start_time = p["start"]
        if p["period"] == end_p:
            end_time = p["end"]
    return start_time, end_time
