"""Local-first SQLite storage for Course Schedule & Academic Calendar (v0.2.8 / R6-R10).

Implements:
- Slot-occurrence exact binding for CourseOverride (time_slot_id)
- Universal Zero Delete compliance (soft deletes: is_deleted, is_revoked)
- Atomic transactions (BEGIN TRANSACTION / COMMIT / ROLLBACK) across all mutations
- Batch atomic transactions for course imports
- Synchronized period-to-time derivation for rescheduled courses
- Pre-existing DB migration and unique index enforcement
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    AcademicCalendar,
    AcademicEvent,
    Course,
    CourseOverride,
    CourseTimeSlot,
    HolidayRule,
    period_range_to_time,
)

DEFAULT_SCHEDULE_DIR = Path.home() / ".personal-ai-workspace" / "schedule"
DEFAULT_DB_PATH = DEFAULT_SCHEDULE_DIR / "schedule.db"


def secure_harden_path(db_path: Path) -> None:
    os.umask(0o077)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(db_path.parent, 0o700)
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            os.chmod(p, 0o600)


class ScheduleStorage:
    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path).expanduser().resolve() if db_path else DEFAULT_DB_PATH
        secure_harden_path(self.db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS calendar_config (
                semester TEXT PRIMARY KEY,
                start_date TEXT NOT NULL,
                total_weeks INTEGER NOT NULL DEFAULT 20,
                teaching_weeks_start INTEGER NOT NULL DEFAULT 1,
                teaching_weeks_end INTEGER NOT NULL DEFAULT 16,
                exam_weeks_start INTEGER NOT NULL DEFAULT 17,
                exam_weeks_end INTEGER NOT NULL DEFAULT 18,
                holidays_json TEXT NOT NULL DEFAULT '[]'
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT NOT NULL DEFAULT '',
                teacher TEXT NOT NULL DEFAULT '',
                classroom TEXT NOT NULL DEFAULT '',
                credits REAL NOT NULL DEFAULT 2.0,
                semester TEXT NOT NULL DEFAULT '2026-2027-1',
                color TEXT NOT NULL DEFAULT '#3b82f6',
                notes TEXT NOT NULL DEFAULT '',
                course_group_id TEXT,
                meeting_url TEXT,
                reminder_minutes INTEGER NOT NULL DEFAULT 15,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS time_slots (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL,
                day_of_week INTEGER NOT NULL,
                start_period INTEGER NOT NULL,
                end_period INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                week_pattern TEXT NOT NULL DEFAULT 'all',
                start_week INTEGER NOT NULL DEFAULT 1,
                end_week INTEGER NOT NULL DEFAULT 16,
                custom_weeks_json TEXT NOT NULL DEFAULT '[]',
                classroom TEXT NOT NULL DEFAULT '',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(course_id) REFERENCES courses(id)
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS overrides (
                id TEXT PRIMARY KEY,
                time_slot_id TEXT NOT NULL,
                course_id TEXT NOT NULL,
                semester TEXT NOT NULL,
                week_number INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                override_type TEXT NOT NULL,
                new_classroom TEXT,
                new_day_of_week INTEGER,
                new_start_period INTEGER,
                new_end_period INTEGER,
                new_start_time TEXT,
                new_end_time TEXT,
                reason TEXT NOT NULL DEFAULT '',
                is_revoked INTEGER NOT NULL DEFAULT 0,
                revoked_at TEXT,
                UNIQUE(time_slot_id, week_number)
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS academic_events (
                id TEXT PRIMARY KEY,
                semester TEXT NOT NULL,
                title TEXT NOT NULL,
                event_type TEXT NOT NULL,
                due_date TEXT NOT NULL,
                due_time TEXT DEFAULT '23:59',
                week_number INTEGER,
                related_course_id TEXT,
                location TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                is_completed INTEGER NOT NULL DEFAULT 0,
                priority TEXT NOT NULL DEFAULT 'medium',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT
            );
            """)

            # Forward migration for pre-existing databases (R6, R8, R10)
            def _ensure_col(table: str, col: str, col_type: str) -> None:
                cur.execute(f"PRAGMA table_info({table});")
                cols = {row[1] for row in cur.fetchall()}
                if col not in cols:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};")

            _ensure_col("courses", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
            _ensure_col("courses", "deleted_at", "TEXT")
            _ensure_col("time_slots", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
            _ensure_col("overrides", "time_slot_id", "TEXT NOT NULL DEFAULT ''")
            _ensure_col("overrides", "is_revoked", "INTEGER NOT NULL DEFAULT 0")
            _ensure_col("overrides", "revoked_at", "TEXT")
            _ensure_col("academic_events", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
            _ensure_col("academic_events", "deleted_at", "TEXT")

            # B3: Enforce unique index on overrides(time_slot_id, week_number) for all DBs
            cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_overrides_slot_week
            ON overrides(time_slot_id, week_number);
            """)

            cur.close()
            secure_harden_path(self.db_path)

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()

    # -------------------------------------------------------------------------
    # Calendar & Week calculation
    # -------------------------------------------------------------------------

    def get_calendar(self, semester: str = "2026-2027-1") -> AcademicCalendar:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM calendar_config WHERE semester = ?;", (semester,))
            row = cur.fetchone()
            cur.close()
            if row is None:
                default_cal = AcademicCalendar(
                    semester=semester,
                    start_date="2026-08-31",
                    total_weeks=20,
                    teaching_weeks_start=1,
                    teaching_weeks_end=16,
                    exam_weeks_start=17,
                    exam_weeks_end=18,
                    holidays=[
                        HolidayRule(
                            name="中秋节",
                            start_date="2026-09-25",
                            end_date="2026-09-27",
                            is_off=True,
                        ),
                        HolidayRule(
                            name="国庆节",
                            start_date="2026-10-01",
                            end_date="2026-10-07",
                            is_off=True,
                        ),
                        HolidayRule(
                            name="元旦",
                            start_date="2027-01-01",
                            end_date="2027-01-03",
                            is_off=True,
                        ),
                    ],
                )
                self.save_calendar(default_cal)
                return default_cal

            holidays = [
                HolidayRule(**h)
                for h in json.loads(row["holidays_json"])
            ]
            return AcademicCalendar(
                semester=row["semester"],
                start_date=row["start_date"],
                total_weeks=int(row["total_weeks"]),
                teaching_weeks_start=int(row["teaching_weeks_start"]),
                teaching_weeks_end=int(row["teaching_weeks_end"]),
                exam_weeks_start=int(row["exam_weeks_start"]),
                exam_weeks_end=int(row["exam_weeks_end"]),
                holidays=holidays,
            )

    def save_calendar(self, cal: AcademicCalendar) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                holidays_json = json.dumps([asdict(h) for h in cal.holidays], ensure_ascii=False)
                cur.execute("""
                INSERT INTO calendar_config (
                    semester, start_date, total_weeks, teaching_weeks_start,
                    teaching_weeks_end, exam_weeks_start, exam_weeks_end, holidays_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(semester) DO UPDATE SET
                    start_date = excluded.start_date,
                    total_weeks = excluded.total_weeks,
                    teaching_weeks_start = excluded.teaching_weeks_start,
                    teaching_weeks_end = excluded.teaching_weeks_end,
                    exam_weeks_start = excluded.exam_weeks_start,
                    exam_weeks_end = excluded.exam_weeks_end,
                    holidays_json = excluded.holidays_json;
                """, (
                    cal.semester, cal.start_date, cal.total_weeks,
                    cal.teaching_weeks_start, cal.teaching_weeks_end,
                    cal.exam_weeks_start, cal.exam_weeks_end, holidays_json
                ))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def compute_current_week(
        self,
        target_date: Optional[date] = None,
        semester: str = "2026-2027-1"
    ) -> Dict[str, Any]:
        cal = self.get_calendar(semester)
        cur_date = target_date or date.today()
        start_d = date.fromisoformat(cal.start_date)

        days_diff = (cur_date - start_d).days
        if days_diff < 0:
            week_num = 0
            phase = "pre_semester"
        else:
            week_num = (days_diff // 7) + 1
            if week_num <= cal.teaching_weeks_end:
                phase = "teaching"
            elif week_num <= cal.exam_weeks_end:
                phase = "exam"
            elif week_num <= cal.total_weeks:
                phase = "post_exam"
            else:
                phase = "vacation"

        monday = cur_date - timedelta(days=cur_date.weekday())
        sunday = monday + timedelta(days=6)

        cur_iso = cur_date.isoformat()
        current_holiday = None
        for h in cal.holidays:
            if h.start_date <= cur_iso <= h.end_date:
                current_holiday = h.name
                break

        return {
            "semester": semester,
            "target_date": cur_iso,
            "day_of_week": cur_date.weekday() + 1,
            "current_week": week_num,
            "phase": phase,
            "phase_label": {
                "pre_semester": "开学前",
                "teaching": f"第 {week_num} 周 · 教学周",
                "exam": f"第 {week_num} 周 · 考试周",
                "post_exam": "学期末",
                "vacation": "假期",
            }.get(phase, f"第 {week_num} 周"),
            "semester_start_date": cal.start_date,
            "week_start_date": monday.isoformat(),
            "week_end_date": sunday.isoformat(),
            "is_holiday": current_holiday is not None,
            "holiday_name": current_holiday,
            "total_weeks": cal.total_weeks,
        }

    # -------------------------------------------------------------------------
    # Course CRUD (Atomic Transactions, Conflict-Free Slots & Soft Deletes)
    # -------------------------------------------------------------------------

    def list_courses(self, semester: str = "2026-2027-1") -> List[Course]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
            SELECT * FROM courses WHERE semester = ? AND is_deleted = 0 ORDER BY name ASC;
            """, (semester,))
            rows = cur.fetchall()
            courses = []
            for r in rows:
                c = self._row_to_course(r)
                c.time_slots = self._get_time_slots(c.id)
                courses.append(c)
            cur.close()
            return courses

    def get_course(self, course_id: str) -> Optional[Course]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM courses WHERE id = ? AND is_deleted = 0;", (course_id,))
            row = cur.fetchone()
            cur.close()
            if row is None:
                return None
            c = self._row_to_course(row)
            c.time_slots = self._get_time_slots(course_id)
            return c

    def _save_course_in_cursor(self, cur: sqlite3.Cursor, course: Course) -> None:
        """Internal worker executing course and slot upsert within an active transaction."""
        cur.execute("""
        INSERT INTO courses (
            id, name, code, teacher, classroom, credits, semester,
            color, notes, course_group_id, meeting_url, reminder_minutes,
            is_deleted, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            code = excluded.code,
            teacher = excluded.teacher,
            classroom = excluded.classroom,
            credits = excluded.credits,
            semester = excluded.semester,
            color = excluded.color,
            notes = excluded.notes,
            course_group_id = excluded.course_group_id,
            meeting_url = excluded.meeting_url,
            reminder_minutes = excluded.reminder_minutes,
            is_deleted = 0,
            deleted_at = NULL;
        """, (
            course.id, course.name, course.code, course.teacher, course.classroom,
            course.credits, course.semester, course.color, course.notes,
            course.course_group_id, course.meeting_url, course.reminder_minutes
        ))

        # Soft delete any slots that are NOT present in the updated course.time_slots
        active_ids = [ts.id for ts in course.time_slots if ts.id]
        if active_ids:
            placeholders = ",".join("?" for _ in active_ids)
            cur.execute(
                f"UPDATE time_slots SET is_deleted = 1 WHERE course_id = ? AND id NOT IN ({placeholders});",
                [course.id, *active_ids]
            )
        else:
            cur.execute("UPDATE time_slots SET is_deleted = 1 WHERE course_id = ?;", (course.id,))

        # Upsert active slots (ON CONFLICT DO UPDATE to avoid UNIQUE constraint crash B1)
        for ts in course.time_slots:
            slot_id = ts.id or f"ts_{uuid.uuid4().hex[:8]}"
            cw_json = json.dumps(ts.custom_weeks)
            cur.execute("""
            INSERT INTO time_slots (
                id, course_id, day_of_week, start_period, end_period,
                start_time, end_time, week_pattern, start_week, end_week,
                custom_weeks_json, classroom, is_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                course_id = excluded.course_id,
                day_of_week = excluded.day_of_week,
                start_period = excluded.start_period,
                end_period = excluded.end_period,
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                week_pattern = excluded.week_pattern,
                start_week = excluded.start_week,
                end_week = excluded.end_week,
                custom_weeks_json = excluded.custom_weeks_json,
                classroom = excluded.classroom,
                is_deleted = 0;
            """, (
                slot_id, course.id, ts.day_of_week, ts.start_period, ts.end_period,
                ts.start_time, ts.end_time, ts.week_pattern, ts.start_week, ts.end_week,
                cw_json, ts.classroom or course.classroom
            ))

    def save_course(self, course: Course) -> None:
        """Saves course and slots in an atomic transaction."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                self._save_course_in_cursor(cur, course)
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def save_courses_batch(self, courses: List[Course]) -> None:
        """R8: Atomic batch transaction. Fails closed and rolls back 100% on any error."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                for c in courses:
                    self._save_course_in_cursor(cur, c)
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def update_meeting_url(self, course_id: str, meeting_url: str) -> None:
        """Atomic targeted update without touching time slots (R12 / B1)."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                cur.execute("UPDATE courses SET meeting_url = ? WHERE id = ? AND is_deleted = 0;", (meeting_url, course_id))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def update_reminder_minutes(self, course_id: str, reminder_minutes: int) -> None:
        """Atomic targeted update without touching time slots (R12 / B1)."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                cur.execute("UPDATE courses SET reminder_minutes = ? WHERE id = ? AND is_deleted = 0;", (reminder_minutes, course_id))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def delete_course(self, course_id: str) -> None:
        """Soft delete (Zero Delete compliance). Never physically drops rows."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                now_iso = datetime.now(timezone.utc).isoformat()
                cur.execute("UPDATE courses SET is_deleted = 1, deleted_at = ? WHERE id = ?;", (now_iso, course_id))
                cur.execute("UPDATE time_slots SET is_deleted = 1 WHERE course_id = ?;", (course_id,))
                cur.execute("UPDATE overrides SET is_revoked = 1, revoked_at = ? WHERE course_id = ?;", (now_iso, course_id))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def _get_time_slots(self, course_id: str) -> List[CourseTimeSlot]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
            SELECT * FROM time_slots WHERE course_id = ? AND is_deleted = 0 ORDER BY day_of_week, start_period;
            """, (course_id,))
            rows = cur.fetchall()
            cur.close()
            return [
                CourseTimeSlot(
                    id=r["id"],
                    course_id=r["course_id"],
                    day_of_week=int(r["day_of_week"]),
                    start_period=int(r["start_period"]),
                    end_period=int(r["end_period"]),
                    start_time=r["start_time"],
                    end_time=r["end_time"],
                    week_pattern=r["week_pattern"],
                    start_week=int(r["start_week"]),
                    end_week=int(r["end_week"]),
                    custom_weeks=json.loads(r["custom_weeks_json"]),
                    classroom=r["classroom"],
                    is_deleted=bool(r["is_deleted"]),
                )
                for r in rows
            ]

    def _row_to_course(self, r: sqlite3.Row) -> Course:
        return Course(
            id=r["id"],
            name=r["name"],
            code=r["code"],
            teacher=r["teacher"],
            classroom=r["classroom"],
            credits=float(r["credits"]),
            semester=r["semester"],
            color=r["color"],
            notes=r["notes"],
            course_group_id=r["course_group_id"],
            meeting_url=r["meeting_url"],
            reminder_minutes=int(r["reminder_minutes"]),
            is_deleted=bool(r["is_deleted"]),
            deleted_at=r["deleted_at"],
        )

    # -------------------------------------------------------------------------
    # Overrides (Occurrence-Level Binding via time_slot_id & Soft Revocation)
    # -------------------------------------------------------------------------

    def add_override(self, override: CourseOverride) -> None:
        """
        Stores an occurrence-level override bound to `time_slot_id`.
        Synchronously derives exact start and end times if periods are rescheduled.
        B9: ON CONFLICT updates id = excluded.id so stored and returned IDs match.
        """
        # R7: Synchronize period and absolute time derivation unconditionally
        if override.new_start_period is not None:
            end_p = override.new_end_period or (override.new_start_period + 1)
            derived_s, derived_e = period_range_to_time(override.new_start_period, end_p)
            override.new_start_time = derived_s
            override.new_end_time = derived_e

        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                cur.execute("""
                INSERT INTO overrides (
                    id, time_slot_id, course_id, semester, week_number, day_of_week,
                    override_type, new_classroom, new_day_of_week,
                    new_start_period, new_end_period, new_start_time, new_end_time,
                    reason, is_revoked, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(time_slot_id, week_number) DO UPDATE SET
                    id = excluded.id,
                    course_id = excluded.course_id,
                    override_type = excluded.override_type,
                    new_classroom = excluded.new_classroom,
                    new_day_of_week = excluded.new_day_of_week,
                    new_start_period = excluded.new_start_period,
                    new_end_period = excluded.new_end_period,
                    new_start_time = excluded.new_start_time,
                    new_end_time = excluded.new_end_time,
                    reason = excluded.reason,
                    is_revoked = 0,
                    revoked_at = NULL;
                """, (
                    override.id, override.time_slot_id, override.course_id, override.semester,
                    override.week_number, override.day_of_week, override.override_type,
                    override.new_classroom, override.new_day_of_week, override.new_start_period,
                    override.new_end_period, override.new_start_time, override.new_end_time, override.reason
                ))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def delete_override(self, override_id: str) -> None:
        """Soft revocation (Zero Delete compliance)."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                now_iso = datetime.now(timezone.utc).isoformat()
                cur.execute("UPDATE overrides SET is_revoked = 1, revoked_at = ? WHERE id = ?;", (now_iso, override_id))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def list_overrides(self, semester: str = "2026-2027-1", week_number: Optional[int] = None) -> List[CourseOverride]:
        with self._lock:
            cur = self._conn.cursor()
            if week_number is not None:
                cur.execute("""
                SELECT * FROM overrides WHERE semester = ? AND week_number = ? AND is_revoked = 0;
                """, (semester, week_number))
            else:
                cur.execute("""
                SELECT * FROM overrides WHERE semester = ? AND is_revoked = 0;
                """, (semester,))
            rows = cur.fetchall()
            cur.close()
            return [
                CourseOverride(
                    id=r["id"],
                    time_slot_id=r["time_slot_id"],
                    course_id=r["course_id"],
                    semester=r["semester"],
                    week_number=int(r["week_number"]),
                    day_of_week=int(r["day_of_week"]),
                    override_type=r["override_type"],
                    new_classroom=r["new_classroom"],
                    new_day_of_week=r["new_day_of_week"],
                    new_start_period=r["new_start_period"],
                    new_end_period=r["new_end_period"],
                    new_start_time=r["new_start_time"],
                    new_end_time=r["new_end_time"],
                    reason=r["reason"],
                    is_revoked=bool(r["is_revoked"]),
                    revoked_at=r["revoked_at"],
                )
                for r in rows
            ]

    # -------------------------------------------------------------------------
    # Effective Week Schedule Resolution (Exact Slot-Occurrence Key)
    # -------------------------------------------------------------------------

    def get_effective_week_schedule(
        self,
        week_number: int,
        semester: str = "2026-2027-1"
    ) -> List[Dict[str, Any]]:
        """
        Calculates the active courses for a given week.
        Keyed on exact `time_slot_id` so other slots on the same day are never contaminated.
        """
        courses = self.list_courses(semester)
        overrides = self.list_overrides(semester, week_number)
        slot_override_map: Dict[str, CourseOverride] = {
            o.time_slot_id: o for o in overrides
        }

        active_slots: List[Dict[str, Any]] = []

        for c in courses:
            for ts in c.time_slots:
                if ts.is_deleted:
                    continue

                # 1. Check week range
                if not (ts.start_week <= week_number <= ts.end_week):
                    continue

                # 2. Check pattern (odd/even/custom/all)
                is_odd_week = (week_number % 2 == 1)
                if ts.week_pattern == "odd" and not is_odd_week:
                    continue
                if ts.week_pattern == "even" and is_odd_week:
                    continue
                if ts.week_pattern == "custom" and ts.custom_weeks and week_number not in ts.custom_weeks:
                    continue

                # 3. Check overrides strictly by time_slot_id (R6)
                ov = slot_override_map.get(ts.id)
                status = "normal"
                effective_day = ts.day_of_week
                effective_start_p = ts.start_period
                effective_end_p = ts.end_period
                effective_start_t = ts.start_time
                effective_end_t = ts.end_time
                effective_room = ts.classroom or c.classroom
                override_reason = ""

                if ov is not None:
                    if ov.override_type == "cancel":
                        status = "cancelled"
                        override_reason = ov.reason or "停课"
                    elif ov.override_type == "relocate":
                        status = "relocated"
                        effective_room = ov.new_classroom or effective_room
                        override_reason = ov.reason or f"调换教室至 {effective_room}"
                    elif ov.override_type == "reschedule":
                        status = "rescheduled"
                        effective_day = ov.new_day_of_week if ov.new_day_of_week is not None else effective_day
                        effective_start_p = ov.new_start_period if ov.new_start_period is not None else effective_start_p
                        effective_end_p = ov.new_end_period if ov.new_end_period is not None else effective_end_p
                        effective_start_t = ov.new_start_time or effective_start_t
                        effective_end_t = ov.new_end_time or effective_end_t
                        if ov.new_classroom:
                            effective_room = ov.new_classroom
                        override_reason = ov.reason or "调课"

                active_slots.append({
                    "time_slot_id": ts.id,
                    "course_id": c.id,
                    "course_name": c.name,
                    "code": c.code,
                    "teacher": c.teacher,
                    "color": c.color,
                    "credits": c.credits,
                    "notes": c.notes,
                    "course_group_id": c.course_group_id,
                    "meeting_url": c.meeting_url,
                    "reminder_minutes": c.reminder_minutes,
                    "day_of_week": effective_day,
                    "start_period": effective_start_p,
                    "end_period": effective_end_p,
                    "start_time": effective_start_t,
                    "end_time": effective_end_t,
                    "classroom": effective_room,
                    "original_classroom": ts.classroom or c.classroom,
                    "week_pattern": ts.week_pattern,
                    "status": status,
                    "override_reason": override_reason,
                    "override_id": ov.id if ov else None,
                })

        # Process any standalone makeup overrides for this week
        for ov in overrides:
            if ov.override_type == "makeup":
                matched_course = next((c for c in courses if c.id == ov.course_id), None)
                if matched_course:
                    start_p = ov.new_start_period or 1
                    end_p = ov.new_end_period or 2
                    s_time, e_time = period_range_to_time(start_p, end_p)
                    active_slots.append({
                        "time_slot_id": ov.time_slot_id,
                        "course_id": matched_course.id,
                        "course_name": matched_course.name,
                        "code": matched_course.code,
                        "teacher": matched_course.teacher,
                        "color": matched_course.color,
                        "credits": matched_course.credits,
                        "notes": matched_course.notes,
                        "course_group_id": matched_course.course_group_id,
                        "meeting_url": matched_course.meeting_url,
                        "reminder_minutes": matched_course.reminder_minutes,
                        "day_of_week": ov.new_day_of_week or ov.day_of_week,
                        "start_period": start_p,
                        "end_period": end_p,
                        "start_time": ov.new_start_time or s_time,
                        "end_time": ov.new_end_time or e_time,
                        "classroom": ov.new_classroom or matched_course.classroom,
                        "original_classroom": matched_course.classroom,
                        "week_pattern": "all",
                        "status": "makeup",
                        "override_reason": ov.reason or "补课",
                        "override_id": ov.id,
                    })

        active_slots.sort(key=lambda s: (s["day_of_week"], s["start_period"]))
        return active_slots

    # -------------------------------------------------------------------------
    # Academic Events (Soft Delete Compliance & Transactions)
    # -------------------------------------------------------------------------

    def list_events(
        self,
        semester: str = "2026-2027-1",
        week_number: Optional[int] = None,
        completed: Optional[bool] = None
    ) -> List[AcademicEvent]:
        with self._lock:
            cur = self._conn.cursor()
            query = "SELECT * FROM academic_events WHERE semester = ? AND is_deleted = 0"
            params: list[Any] = [semester]
            if week_number is not None:
                query += " AND week_number = ?"
                params.append(week_number)
            if completed is not None:
                query += " AND is_completed = ?"
                params.append(1 if completed else 0)
            query += " ORDER BY due_date ASC, due_time ASC;"
            cur.execute(query, params)
            rows = cur.fetchall()
            cur.close()
            return [
                AcademicEvent(
                    id=r["id"],
                    semester=r["semester"],
                    title=r["title"],
                    event_type=r["event_type"],
                    due_date=r["due_date"],
                    due_time=r["due_time"],
                    week_number=r["week_number"],
                    related_course_id=r["related_course_id"],
                    location=r["location"],
                    notes=r["notes"],
                    is_completed=bool(r["is_completed"]),
                    priority=r["priority"],
                    is_deleted=bool(r["is_deleted"]),
                    deleted_at=r["deleted_at"],
                )
                for r in rows
            ]

    def save_event(self, event: AcademicEvent) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                cur.execute("""
                INSERT INTO academic_events (
                    id, semester, title, event_type, due_date, due_time,
                    week_number, related_course_id, location, notes, is_completed,
                    priority, is_deleted, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    event_type = excluded.event_type,
                    due_date = excluded.due_date,
                    due_time = excluded.due_time,
                    week_number = excluded.week_number,
                    related_course_id = excluded.related_course_id,
                    location = excluded.location,
                    notes = excluded.notes,
                    is_completed = excluded.is_completed,
                    priority = excluded.priority,
                    is_deleted = 0,
                    deleted_at = NULL;
                """, (
                    event.id, event.semester, event.title, event.event_type,
                    event.due_date, event.due_time, event.week_number,
                    event.related_course_id, event.location, event.notes,
                    1 if event.is_completed else 0, event.priority
                ))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def toggle_event_completed(self, event_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                cur.execute("SELECT is_completed FROM academic_events WHERE id = ? AND is_deleted = 0;", (event_id,))
                row = cur.fetchone()
                if not row:
                    cur.execute("COMMIT;")
                    return False
                new_val = 0 if row["is_completed"] else 1
                cur.execute("UPDATE academic_events SET is_completed = ? WHERE id = ?;", (new_val, event_id))
                cur.execute("COMMIT;")
                return bool(new_val)
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def delete_event(self, event_id: str) -> None:
        """Soft delete (Zero Delete compliance)."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION;")
                now_iso = datetime.now(timezone.utc).isoformat()
                cur.execute("UPDATE academic_events SET is_deleted = 1, deleted_at = ? WHERE id = ?;", (now_iso, event_id))
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    # -------------------------------------------------------------------------
    # Course Teachers Registry for WeCom Filtering
    # -------------------------------------------------------------------------

    def get_course_teachers(self, semester: str = "2026-2027-1") -> List[str]:
        courses = self.list_courses(semester)
        teachers = set()
        for c in courses:
            if c.teacher:
                teachers.add(c.teacher.strip())
        return sorted(teachers)
