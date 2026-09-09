"""FastAPI Router for Course Schedule & Academic Calendar (v0.2.8 / R1-R13)."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.app.config import load_config
from backend.app.schedule.importer import (
    import_schedule_from_obsidian_vault,
    parse_markdown_schedule_table,
)
from backend.app.schedule.models import (
    AcademicCalendar,
    AcademicEvent,
    Course,
    CourseOverride,
    CourseTimeSlot,
    HolidayRule,
    period_range_to_time,
)
from backend.app.schedule.reminders import get_upcoming_reminders
from backend.app.schedule.storage import ScheduleStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedule", tags=["Schedule"])

_storage: Optional[ScheduleStorage] = None
_storage_lock = threading.Lock()
# Sol P1/P2 修复：导入尝试只做一次（失败也记为已尝试），否则 vault 无课表时
# 每个请求都全 vault 扫描；懒初始化并发首请求会构造两条泄漏的 SQLite 连接
_import_attempted = False


def get_schedule_storage() -> ScheduleStorage:
    global _storage, _import_attempted
    with _storage_lock:
        if _storage is None:
            _storage = ScheduleStorage()
        if not _import_attempted and len(_storage.list_courses()) == 0:
            _import_attempted = True
            try:
                cfg = load_config()
                courses = import_schedule_from_obsidian_vault(cfg.vault_path)
                _storage.save_courses_batch(courses)
            except Exception:
                logger.exception("schedule import from obsidian vault failed")  # 失败可见，绝不静默
    return _storage


# -----------------------------------------------------------------------------
# Request Schemas
# -----------------------------------------------------------------------------


class TimeSlotIn(BaseModel):
    id: Optional[str] = None
    day_of_week: int = Field(..., ge=1, le=7)
    start_period: int = Field(..., ge=1, le=11)
    end_period: int = Field(..., ge=1, le=11)
    start_time: str
    end_time: str
    week_pattern: str = "all"
    start_week: int = 1
    end_week: int = 16
    custom_weeks: List[int] = []
    classroom: str = ""


class CourseIn(BaseModel):
    id: Optional[str] = None
    name: str
    code: str = ""
    teacher: str = ""
    classroom: str = ""
    credits: float = 2.0
    semester: str = "2026-2027-1"
    color: str = "#3b82f6"
    notes: str = ""
    course_group_id: Optional[str] = None
    meeting_url: Optional[str] = None
    reminder_minutes: int = 15
    time_slots: List[TimeSlotIn] = []


class OverrideIn(BaseModel):
    time_slot_id: str
    course_id: str
    semester: str = "2026-2027-1"
    week_number: int = Field(..., ge=1, le=30)
    day_of_week: int = Field(..., ge=1, le=7)
    override_type: str  # "cancel" | "reschedule" | "relocate" | "makeup"
    new_classroom: Optional[str] = None
    new_day_of_week: Optional[int] = Field(None, ge=1, le=7)
    new_start_period: Optional[int] = Field(None, ge=1, le=11)
    new_end_period: Optional[int] = Field(None, ge=1, le=11)
    new_start_time: Optional[str] = None
    new_end_time: Optional[str] = None
    reason: str = ""


class EventIn(BaseModel):
    id: Optional[str] = None
    semester: str = "2026-2027-1"
    title: str
    event_type: str = "assignment"
    due_date: str
    due_time: Optional[str] = "23:59"
    week_number: Optional[int] = None
    related_course_id: Optional[str] = None
    location: str = ""
    notes: str = ""
    priority: str = "medium"


class CalendarIn(BaseModel):
    semester: str = "2026-2027-1"
    start_date: str = "2026-08-31"
    total_weeks: int = 20
    teaching_weeks_start: int = 1
    teaching_weeks_end: int = 16
    exam_weeks_start: int = 17
    exam_weeks_end: int = 18
    holidays: List[Dict[str, Any]] = []


class MarkdownImportIn(BaseModel):
    markdown: str
    semester: str = "2026-2027-1"


class MeetingUrlIn(BaseModel):
    meeting_url: str


class ReminderMinutesIn(BaseModel):
    reminder_minutes: int = Field(..., ge=5, le=120)


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------


@router.get("/current-week")
def get_current_week_info(semester: str = Query("2026-2027-1")) -> Dict[str, Any]:
    storage = get_schedule_storage()
    return storage.compute_current_week(semester=semester)


@router.get("/week/{week_number}")
def get_week_schedule(week_number: int, semester: str = Query("2026-2027-1")) -> Dict[str, Any]:
    storage = get_schedule_storage()
    slots = storage.get_effective_week_schedule(week_number=week_number, semester=semester)
    week_info = storage.compute_current_week(semester=semester)
    return {
        "week_number": week_number,
        "semester": semester,
        "is_current_week": (week_number == week_info["current_week"]),
        "slots": slots,
        "total_slots": len(slots),
    }


@router.get("/courses")
def list_courses(semester: str = Query("2026-2027-1")) -> Dict[str, Any]:
    storage = get_schedule_storage()
    courses = storage.list_courses(semester=semester)
    return {
        "courses": [asdict(c) for c in courses],
        "total": len(courses),
    }


@router.get("/course/{course_id}")
def get_course(course_id: str) -> Dict[str, Any]:
    storage = get_schedule_storage()
    course = storage.get_course(course_id)
    if not course:
        raise HTTPException(status_code=404, detail="课程不存在")
    return asdict(course)


@router.post("/course")
def create_course(payload: CourseIn) -> Dict[str, Any]:
    storage = get_schedule_storage()
    # B7: Enforce semester namespace on ID
    sem_clean = payload.semester.replace("-", "_")
    if not payload.id or not payload.id.startswith(f"crs_{sem_clean}_"):
        cid = f"crs_{sem_clean}_{uuid.uuid4().hex[:8]}"
    else:
        cid = payload.id

    slots = [
        CourseTimeSlot(
            id=ts.id or f"ts_{uuid.uuid4().hex[:8]}",
            course_id=cid,
            day_of_week=ts.day_of_week,
            start_period=ts.start_period,
            end_period=ts.end_period,
            start_time=ts.start_time,
            end_time=ts.end_time,
            week_pattern=ts.week_pattern,
            start_week=ts.start_week,
            end_week=ts.end_week,
            custom_weeks=ts.custom_weeks,
            classroom=ts.classroom or payload.classroom,
        )
        for ts in payload.time_slots
    ]
    course = Course(
        id=cid,
        name=payload.name,
        code=payload.code,
        teacher=payload.teacher,
        classroom=payload.classroom,
        credits=payload.credits,
        semester=payload.semester,
        color=payload.color,
        notes=payload.notes,
        course_group_id=payload.course_group_id,
        meeting_url=payload.meeting_url,
        reminder_minutes=payload.reminder_minutes,
        time_slots=slots,
    )
    storage.save_course(course)
    return {"status": "ok", "course": asdict(course)}


@router.put("/course/{course_id}")
def update_course(course_id: str, payload: CourseIn) -> Dict[str, Any]:
    storage = get_schedule_storage()
    existing = storage.get_course(course_id)
    if not existing:
        raise HTTPException(status_code=404, detail="课程不存在")
    if payload.semester != existing.semester:
        # 学期不一致会走 id 前缀检查失败分支、插入重复课程而非更新（Sol P2）
        raise HTTPException(status_code=400, detail="不允许修改课程学期")
    payload.id = course_id
    return create_course(payload)


@router.put("/course/{course_id}/meeting")
def update_course_meeting(course_id: str, payload: MeetingUrlIn) -> Dict[str, Any]:
    """Atomic update of meeting URL without touching slots (B1 / R12)."""
    storage = get_schedule_storage()
    course = storage.get_course(course_id)
    if not course:
        raise HTTPException(status_code=404, detail="课程不存在")
    storage.update_meeting_url(course_id, payload.meeting_url)
    return {"status": "ok", "course_id": course_id, "meeting_url": payload.meeting_url}


@router.put("/course/{course_id}/reminder")
def update_course_reminder(course_id: str, payload: ReminderMinutesIn) -> Dict[str, Any]:
    """Atomic update of reminder threshold without touching slots (B1 / R12)."""
    storage = get_schedule_storage()
    course = storage.get_course(course_id)
    if not course:
        raise HTTPException(status_code=404, detail="课程不存在")
    storage.update_reminder_minutes(course_id, payload.reminder_minutes)
    return {"status": "ok", "course_id": course_id, "reminder_minutes": payload.reminder_minutes}


@router.delete("/course/{course_id}")
def delete_course(course_id: str) -> Dict[str, Any]:
    """Soft delete course (Zero Delete compliance)."""
    storage = get_schedule_storage()
    if not storage.get_course(course_id):
        raise HTTPException(status_code=404, detail="课程不存在")
    storage.delete_course(course_id)
    return {"status": "ok", "soft_deleted": course_id}


@router.post("/override")
def add_override(payload: OverrideIn) -> Dict[str, Any]:
    """Occurrence-level temporary override (cancel, relocate, reschedule, makeup)."""
    storage = get_schedule_storage()
    oid = f"ov_{uuid.uuid4().hex[:10]}"

    # B5: Unconditional time derivation when start_period is given
    derived_s = payload.new_start_time
    derived_e = payload.new_end_time
    if payload.new_start_period is not None:
        end_p = payload.new_end_period or (payload.new_start_period + 1)
        derived_s, derived_e = period_range_to_time(payload.new_start_period, end_p)

    override = CourseOverride(
        id=oid,
        time_slot_id=payload.time_slot_id,
        course_id=payload.course_id,
        semester=payload.semester,
        week_number=payload.week_number,
        day_of_week=payload.day_of_week,
        override_type=payload.override_type,
        new_classroom=payload.new_classroom,
        new_day_of_week=payload.new_day_of_week,
        new_start_period=payload.new_start_period,
        new_end_period=payload.new_end_period,
        new_start_time=derived_s,
        new_end_time=derived_e,
        reason=payload.reason,
    )
    storage.add_override(override)
    return {"status": "ok", "override": asdict(override)}


@router.delete("/override/{override_id}")
def delete_override(override_id: str) -> Dict[str, Any]:
    """Soft revocation of override (Zero Delete compliance)."""
    storage = get_schedule_storage()
    storage.delete_override(override_id)
    return {"status": "ok", "soft_revoked": override_id}


@router.get("/calendar")
def get_calendar(semester: str = Query("2026-2027-1")) -> Dict[str, Any]:
    storage = get_schedule_storage()
    cal = storage.get_calendar(semester)
    return asdict(cal)


@router.post("/calendar")
def save_calendar(payload: CalendarIn) -> Dict[str, Any]:
    storage = get_schedule_storage()
    holidays = [HolidayRule(**h) for h in payload.holidays]
    cal = AcademicCalendar(
        semester=payload.semester,
        start_date=payload.start_date,
        total_weeks=payload.total_weeks,
        teaching_weeks_start=payload.teaching_weeks_start,
        teaching_weeks_end=payload.teaching_weeks_end,
        exam_weeks_start=payload.exam_weeks_start,
        exam_weeks_end=payload.exam_weeks_end,
        holidays=holidays,
    )
    storage.save_calendar(cal)
    return {"status": "ok", "calendar": asdict(cal)}


@router.get("/events")
def list_events(
    semester: str = Query("2026-2027-1"),
    week_number: Optional[int] = Query(None),
    completed: Optional[bool] = Query(None),
) -> Dict[str, Any]:
    storage = get_schedule_storage()
    events = storage.list_events(semester=semester, week_number=week_number, completed=completed)
    return {"events": [asdict(e) for e in events], "total": len(events)}


@router.post("/event")
def create_event(payload: EventIn) -> Dict[str, Any]:
    storage = get_schedule_storage()
    eid = payload.id or f"evt_{uuid.uuid4().hex[:10]}"
    evt = AcademicEvent(
        id=eid,
        semester=payload.semester,
        title=payload.title,
        event_type=payload.event_type,
        due_date=payload.due_date,
        due_time=payload.due_time,
        week_number=payload.week_number,
        related_course_id=payload.related_course_id,
        location=payload.location,
        notes=payload.notes,
        is_completed=False,
        priority=payload.priority,
    )
    storage.save_event(evt)
    return {"status": "ok", "event": asdict(evt)}


@router.put("/event/{event_id}/toggle")
def toggle_event(event_id: str) -> Dict[str, Any]:
    storage = get_schedule_storage()
    completed = storage.toggle_event_completed(event_id)
    return {"status": "ok", "event_id": event_id, "is_completed": completed}


@router.delete("/event/{event_id}")
def delete_event(event_id: str) -> Dict[str, Any]:
    """Soft delete event (Zero Delete compliance)."""
    storage = get_schedule_storage()
    storage.delete_event(event_id)
    return {"status": "ok", "soft_deleted": event_id}


@router.get("/reminders/upcoming")
def get_reminders(
    lookahead_minutes: int = Query(60, ge=5, le=180), semester: str = Query("2026-2027-1")
) -> Dict[str, Any]:
    storage = get_schedule_storage()
    reminders = get_upcoming_reminders(
        storage=storage, lookahead_minutes=lookahead_minutes, semester=semester
    )
    return {"reminders": reminders, "count": len(reminders)}


@router.post("/import/obsidian")
def import_from_obsidian_vault(semester: str = Query("2026-2027-1")) -> Dict[str, Any]:
    """B4: 1-click import from 课表.md executed in an atomic batch transaction."""
    from ..state import get_cfg  # 延迟导入避免测试环境未初始化 lifespan

    storage = get_schedule_storage()
    try:
        cfg = get_cfg()
    except RuntimeError:
        cfg = load_config()
    courses = import_schedule_from_obsidian_vault(cfg.vault_path, semester=semester)
    storage.save_courses_batch(courses)
    return {
        "status": "ok",
        "imported_courses": len(courses),
        "courses": [asdict(c) for c in courses],
    }


@router.post("/import/markdown")
def import_from_markdown(payload: MarkdownImportIn) -> Dict[str, Any]:
    """B4: Import from markdown executed in an atomic batch transaction."""
    storage = get_schedule_storage()
    courses = parse_markdown_schedule_table(payload.markdown, semester=payload.semester)
    storage.save_courses_batch(courses)
    return {
        "status": "ok",
        "imported_courses": len(courses),
        "courses": [asdict(c) for c in courses],
    }


@router.get("/teachers")
def list_teachers(semester: str = Query("2026-2027-1")) -> Dict[str, Any]:
    storage = get_schedule_storage()
    teachers = storage.get_course_teachers(semester=semester)
    return {"teachers": teachers, "count": len(teachers)}
