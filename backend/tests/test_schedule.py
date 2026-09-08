"""Comprehensive Test Suite for Schedule, Academic Calendar & Focus Rules."""

import json
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.im.rules import evaluate_focus_rules
from backend.app.main import app
from backend.app.schedule.importer import parse_markdown_schedule_table
from backend.app.schedule.models import (
    AcademicCalendar,
    AcademicEvent,
    Course,
    CourseOverride,
    CourseTimeSlot,
    HolidayRule,
)
from backend.app.schedule.reminders import get_upcoming_reminders
from backend.app.schedule.storage import ScheduleStorage


# -----------------------------------------------------------------------------
# 1. Week & Academic Calendar Calculation Tests
# -----------------------------------------------------------------------------

def test_current_week_and_phase_calculation():
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")
        cal = AcademicCalendar(
            semester="2026-2027-1",
            start_date="2026-08-31",
            total_weeks=20,
            teaching_weeks_start=1,
            teaching_weeks_end=16,
            exam_weeks_start=17,
            exam_weeks_end=18,
            holidays=[
                HolidayRule(name="国庆节", start_date="2026-10-01", end_date="2026-10-07")
            ]
        )
        storage.save_calendar(cal)

        # 2026-09-08 is Tuesday of Week 2
        w2 = storage.compute_current_week(target_date=date(2026, 9, 8), semester="2026-2027-1")
        assert w2["current_week"] == 2
        assert w2["day_of_week"] == 2  # Tuesday
        assert w2["phase"] == "teaching"
        assert w2["is_holiday"] is False

        # 2026-08-25 is before semester starts
        w_pre = storage.compute_current_week(target_date=date(2026, 8, 25), semester="2026-2027-1")
        assert w_pre["current_week"] == 0
        assert w_pre["phase"] == "pre_semester"

        # 2026-10-03 is National Day Holiday (Week 5)
        w_hol = storage.compute_current_week(target_date=date(2026, 10, 3), semester="2026-2027-1")
        assert w_hol["is_holiday"] is True
        assert w_hol["holiday_name"] == "国庆节"

        # 2026-12-28 is Exam Week (Week 18)
        w_exam = storage.compute_current_week(target_date=date(2026, 12, 28), semester="2026-2027-1")
        assert w_exam["current_week"] == 18
        assert w_exam["phase"] == "exam"

        storage.close()


# -----------------------------------------------------------------------------
# 2. Markdown Importer & Week Pattern Filter Tests
# -----------------------------------------------------------------------------

SAMPLE_SCHEDULE_MD = """
## 已排课课程

| 星期 | 节次 | 时间 | 课程 | 教学班 | 周次 | 教师 | 教室／场地 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 星期一 | 第1–2节 | 08:00–09:40 | 数字电子技术(B) | `[209100035618]-01` | 2–16周（双） | 田莎莎 | 15110 |
| 星期一 | 第3–4节 | 10:00–11:40 | 概率论与数理统计 | `[2101000112]-06` | 1–14周 | 谭永荣 | 11309 |
| 星期一 | 第9–11节 | 18:40–21:05 | 数字电子技术(B)（实验） | `[209100035618S]-01` | 9–15周（单） | 田莎莎 | S090307 |
"""

def test_markdown_importer_and_effective_schedule():
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")
        courses = parse_markdown_schedule_table(SAMPLE_SCHEDULE_MD, semester="2026-2027-1")
        assert len(courses) == 3

        for c in courses:
            storage.save_course(c)

        # Week 2 (Even week):
        # - 数字电子技术(B) (2-16周双) SHOULD BE ACTIVE
        # - 概率论与数理统计 (1-14周) SHOULD BE ACTIVE
        # - 数字电子技术(B)（实验） (9-15周单) SHOULD BE INACTIVE (wrong week range and pattern)
        w2_slots = storage.get_effective_week_schedule(2, semester="2026-2027-1")
        w2_names = [s["course_name"] for s in w2_slots]
        assert "数字电子技术(B)" in w2_names
        assert "概率论与数理统计" in w2_names
        assert "数字电子技术(B)（实验）" not in w2_names

        # Week 3 (Odd week):
        # - 数字电子技术(B) (2-16周双) SHOULD BE INACTIVE (odd week)
        # - 概率论与数理统计 (1-14周) SHOULD BE ACTIVE
        w3_slots = storage.get_effective_week_schedule(3, semester="2026-2027-1")
        w3_names = [s["course_name"] for s in w3_slots]
        assert "数字电子技术(B)" not in w3_names
        assert "概率论与数理统计" in w3_names

        # Week 9 (Odd week):
        # - 数字电子技术(B)（实验） (9-15周单) SHOULD BE ACTIVE
        w9_slots = storage.get_effective_week_schedule(9, semester="2026-2027-1")
        w9_names = [s["course_name"] for s in w9_slots]
        assert "数字电子技术(B)（实验）" in w9_names

        storage.close()


# -----------------------------------------------------------------------------
# 3. Single-Week Temporary Overrides Isolation Tests
# -----------------------------------------------------------------------------

def test_single_week_overrides_isolation():
    """
    Temporary override for Week 2 (relocate / cancel) must NOT bleed into Week 3 or Week 1.
    """
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")
        courses = parse_markdown_schedule_table(SAMPLE_SCHEDULE_MD, semester="2026-2027-1")
        for c in courses:
            storage.save_course(c)

        prob_course = next(c for c in courses if "概率论" in c.name)

        # Relocate Week 2's classroom to 15204
        ov = CourseOverride(
            id="ov_week2_relocate",
            time_slot_id=prob_course.time_slots[0].id,
            course_id=prob_course.id,
            semester="2026-2027-1",
            week_number=2,
            day_of_week=1,
            override_type="relocate",
            new_classroom="15204",
            reason="临时更换多媒体教室"
        )
        storage.add_override(ov)

        # Assert Week 2 has relocated classroom
        w2_slots = storage.get_effective_week_schedule(2, semester="2026-2027-1")
        prob_w2 = next(s for s in w2_slots if s["course_id"] == prob_course.id)
        assert prob_w2["classroom"] == "15204"
        assert prob_w2["status"] == "relocated"
        assert "临时更换" in prob_w2["override_reason"]

        # Assert Week 3 retains the original classroom 11309!
        w3_slots = storage.get_effective_week_schedule(3, semester="2026-2027-1")
        prob_w3 = next(s for s in w3_slots if s["course_id"] == prob_course.id)
        assert prob_w3["classroom"] == "11309"
        assert prob_w3["status"] == "normal"

        storage.close()


# -----------------------------------------------------------------------------
# 4. Upcoming Class Reminders Tests
# -----------------------------------------------------------------------------

def test_upcoming_reminders_and_override_synchronization():
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")
        courses = parse_markdown_schedule_table(SAMPLE_SCHEDULE_MD, semester="2026-2027-1")
        for c in courses:
            storage.save_course(c)

        # Assume reference time is 2026-09-08 (Tuesday) 09:50 (10 mins before 10:00 class)
        # Note: on Day 2 (Tuesday), SAMPLE_SCHEDULE_MD has no class, let's test Day 1 (Monday) 09:45
        ref_monday = datetime(2026, 9, 7, 9, 45)  # 15 minutes before 10:00 概率论

        reminders = get_upcoming_reminders(storage, ref_dt=ref_monday, lookahead_minutes=30)
        assert len(reminders) >= 1
        rem = next(r for r in reminders if "概率论" in r["course_name"])
        assert rem["classroom"] == "11309"
        assert rem["minutes_until_start"] == 15

        # Test: Cancel this class in week 2
        prob_course = next(c for c in courses if "概率论" in c.name)
        storage.add_override(CourseOverride(
            id="ov_cancel_prob",
            time_slot_id=prob_course.time_slots[0].id,
            course_id=prob_course.id,
            semester="2026-2027-1",
            week_number=2,
            day_of_week=1,
            override_type="cancel",
            reason="老师开会停课一次"
        ))

        # Cancelled class must NOT produce reminder!
        reminders_after_cancel = get_upcoming_reminders(storage, ref_dt=ref_monday, lookahead_minutes=30)
        assert not any("概率论" in r["course_name"] for r in reminders_after_cancel)

        storage.close()


# -----------------------------------------------------------------------------
# 5. Integrated Academic Events Tests
# -----------------------------------------------------------------------------

def test_integrated_academic_events():
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")

        evt1 = AcademicEvent(
            id="evt_1",
            semester="2026-2027-1",
            title="数据结构第一次上机实验",
            event_type="lab",
            due_date="2026-09-12",
            due_time="17:00",
            week_number=2,
            location="S090204",
            priority="high",
        )
        storage.save_event(evt1)

        events = storage.list_events(semester="2026-2027-1", week_number=2)
        assert len(events) == 1
        assert events[0].title == "数据结构第一次上机实验"
        assert events[0].is_completed is False

        # Toggle completion
        completed = storage.toggle_event_completed("evt_1")
        assert completed is True

        events_uncompleted = storage.list_events(semester="2026-2027-1", completed=False)
        assert len(events_uncompleted) == 0

        storage.close()


# -----------------------------------------------------------------------------
# 6. Focus & Attention Rules Unit Tests (User Specifications)
# -----------------------------------------------------------------------------

def test_user_specific_focus_rules_matrix():
    # 6.1 QQ: 人工2502班通知群 (全量必看高优待办)
    tags, reasons = evaluate_focus_rules(
        channel_name="人工2502班通知群",
        channel_type="group",
        source="qq",
        sender_name="学习委员",
        text="明天调课通知"
    )
    assert "class_must_read" in tags
    assert "人工2502班通知群" in reasons[0]

    # 6.2 QQ: 乌鸦像写字台 (本人备忘，待同步 Obsidian)
    tags, reasons = evaluate_focus_rules(
        channel_name="乌鸦像写字台",
        channel_type="direct",
        source="qq",
        sender_name="乌鸦像写字台",
        text="记录：研读 Transformer 论文第 4 节"
    )
    assert "self_memo_obsidian" in tags
    assert "Obsidian" in reasons[0]

    # 6.3 QQ: 康老师 (领导，每条必处理)
    tags, reasons = evaluate_focus_rules(
        channel_name="康老师",
        channel_type="direct",
        source="qq",
        sender_name="康老师",
        text="浩岚，下午两点到实验室开组会"
    )
    assert "leader_urgent_todo" in tags
    assert "康老师" in reasons[0]

    # 6.3B QQ: 康老师在班级群发言 -> 必须依然是最高优先级 leader_urgent_todo！(R3)
    tags_kang_class, reasons_kang_class = evaluate_focus_rules(
        channel_name="人工2502班通知群",
        channel_type="group",
        source="qq",
        sender_name="康老师",
        text="大家下午好，关于选课有一点说明"
    )
    assert "leader_urgent_todo" in tags_kang_class
    assert "康老师" in reasons_kang_class[0]

    # 6.4 QQ: 2026新思路中高层群 (工作群重点)
    tags, reasons = evaluate_focus_rules(
        channel_name="2026新思路中高层群",
        channel_type="group",
        source="qq",
        sender_name="行政主管",
        text="请各组提交本周周报"
    )
    assert "work_group_focus" in tags
    assert "新思路" in reasons[0]

    # 6.5 QQ: 普通群聊 (默认折叠，不打入 focus 标签) (R4)
    tags, reasons = evaluate_focus_rules(
        channel_name="王者荣耀开黑群",
        channel_type="group",
        source="qq",
        sender_name="张三",
        text="今晚来一把"
    )
    assert tags == []  # Folded groups must have EMPTY tags so they are excluded from focus feed!

    # 6.6 微信: 普通群聊未处理消息全量登记待办 (R4)
    tags_wx_grp, reasons_wx_grp = evaluate_focus_rules(
        channel_name="骑行爱好者俱乐部",
        channel_type="group",
        source="wechat",
        sender_name="李四",
        text="周六环湖骑行报名"
    )
    assert "wechat_todo" in tags_wx_grp

    # 6.7 企微: 课程群内仅关注任课老师
    # Case A: 老师发言 -> 登记为 course_teacher_notice
    tags_t, reasons_t = evaluate_focus_rules(
        channel_name="大学物理B(2)-2026-2027-1",
        channel_type="group",
        source="wecom",
        sender_name="谢金翠",  # 课表中的大物任课老师
        text="本周五由于校运动会停课一次"
    )
    assert "course_teacher_notice" in tags_t
    assert "谢金翠" in reasons_t[0]

    # Case B: 同学发言 -> 不登记！即使携带 @本人 也不得穿透泄露！(R5)
    tags_s, reasons_s = evaluate_focus_rules(
        channel_name="大学物理B(2)-2026-2027-1",
        channel_type="group",
        source="wecom",
        sender_name="李同学",
        text="收到老师 @袁浩岚",
        mentions=[{"is_self": True}]
    )
    assert "course_teacher_notice" not in tags_s
    assert "mention_self" not in tags_s
    assert len(tags_s) == 0

    # 6.8 企微: 个人私聊必须登记待处理
    tags_direct, reasons_direct = evaluate_focus_rules(
        channel_name="辅导员刘老师",
        channel_type="direct",
        source="wecom",
        sender_name="辅导员刘老师",
        text="请提交综合素质测评表"
    )
    assert "wecom_direct_todo" in tags_direct


# -----------------------------------------------------------------------------
# 7. FastAPI Schedule Endpoints End-to-End
# -----------------------------------------------------------------------------

def test_schedule_api_endpoints():
    client = TestClient(app)

    # 1. GET current-week
    r1 = client.get("/api/schedule/current-week")
    assert r1.status_code == 200
    data1 = r1.json()
    assert "current_week" in data1
    assert "phase" in data1

    # 2. GET week schedule
    r2 = client.get("/api/schedule/week/2")
    assert r2.status_code == 200
    data2 = r2.json()
    assert "slots" in data2
    assert len(data2["slots"]) > 0

    # 3. GET courses
    r3 = client.get("/api/schedule/courses")
    assert r3.status_code == 200
    data3 = r3.json()
    assert data3["total"] >= 10

    # 4. POST event
    evt_payload = {
        "title": "深度学习第一次作业",
        "event_type": "assignment",
        "due_date": "2026-09-18",
        "due_time": "23:59",
        "week_number": 3,
        "location": "学习通平台",
        "priority": "high",
    }
    r4 = client.post("/api/schedule/event", json=evt_payload)
    assert r4.status_code == 200
    eid = r4.json()["event"]["id"]

    # 5. GET events & toggle
    r5 = client.get("/api/schedule/events?week_number=3")
    assert r5.status_code == 200
    assert any(e["id"] == eid for e in r5.json()["events"])

    r6 = client.put(f"/api/schedule/event/{eid}/toggle")
    assert r6.status_code == 200
    assert r6.json()["is_completed"] is True

    # 6. DELETE event (Soft Delete compliance)
    r7 = client.delete(f"/api/schedule/event/{eid}")
    assert r7.status_code == 200
    assert r7.json()["status"] == "ok"


# -----------------------------------------------------------------------------
# 8. Same-Day Multiple Slots Override Isolation (R6)
# -----------------------------------------------------------------------------

def test_same_day_multiple_slots_override_isolation():
    """
    R6: A course with two slots on the same day (e.g. slot 1 in morning,
    slot 2 in evening). Applying an override to slot 1 MUST NOT affect slot 2!
    """
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")
        course = Course(
            id="crs_multi_slot",
            name="数字电子技术(B)",
            semester="2026-2027-1",
            classroom="15110",
            time_slots=[
                CourseTimeSlot(
                    id="ts_morning_1",
                    course_id="crs_multi_slot",
                    day_of_week=1,
                    start_period=1,
                    end_period=2,
                    start_time="08:00",
                    end_time="09:40",
                    start_week=1,
                    end_week=16,
                    classroom="15110"
                ),
                CourseTimeSlot(
                    id="ts_evening_2",
                    course_id="crs_multi_slot",
                    day_of_week=1,
                    start_period=9,
                    end_period=11,
                    start_time="18:40",
                    end_time="21:05",
                    start_week=1,
                    end_week=16,
                    classroom="S090307"
                ),
            ]
        )
        storage.save_course(course)

        # Relocate ONLY the morning slot (ts_morning_1) for Week 2
        ov = CourseOverride(
            id="ov_morning_only",
            time_slot_id="ts_morning_1",
            course_id="crs_multi_slot",
            semester="2026-2027-1",
            week_number=2,
            day_of_week=1,
            override_type="relocate",
            new_classroom="15204",
            reason="上午改至15204"
        )
        storage.add_override(ov)

        slots = storage.get_effective_week_schedule(2, semester="2026-2027-1")
        morning_slot = next(s for s in slots if s["time_slot_id"] == "ts_morning_1")
        evening_slot = next(s for s in slots if s["time_slot_id"] == "ts_evening_2")

        # Morning slot is relocated
        assert morning_slot["classroom"] == "15204"
        assert morning_slot["status"] == "relocated"

        # Evening slot MUST RETAIN its original classroom S090307!
        assert evening_slot["classroom"] == "S090307"
        assert evening_slot["status"] == "normal"

        storage.close()


# -----------------------------------------------------------------------------
# 9. Period & Absolute Time Synchronization in Reschedule (R7)
# -----------------------------------------------------------------------------

def test_reschedule_period_and_time_synchronization():
    """
    R7: Rescheduling a class to Period 5-6 must automatically derive start_time='14:10'
    and end_time='15:50', and upcoming reminder must trigger at 14:10, NOT original 08:00!
    """
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")
        course = Course(
            id="crs_resched_test",
            name="数据结构与算法",
            semester="2026-2027-1",
            classroom="11413",
            time_slots=[
                CourseTimeSlot(
                    id="ts_resched_1",
                    course_id="crs_resched_test",
                    day_of_week=2,
                    start_period=1,
                    end_period=2,
                    start_time="08:00",
                    end_time="09:40",
                    start_week=1,
                    end_week=16,
                    classroom="11413"
                )
            ]
        )
        storage.save_course(course)

        # Reschedule to Period 5-6 (afternoon)
        ov = CourseOverride(
            id="ov_resched_1",
            time_slot_id="ts_resched_1",
            course_id="crs_resched_test",
            semester="2026-2027-1",
            week_number=2,
            day_of_week=2,
            override_type="reschedule",
            new_day_of_week=2,
            new_start_period=5,
            new_end_period=6,
            reason="调至下午第5-6节"
        )
        storage.add_override(ov)

        slots = storage.get_effective_week_schedule(2, semester="2026-2027-1")
        resched_slot = slots[0]
        assert resched_slot["start_period"] == 5
        assert resched_slot["end_period"] == 6
        assert resched_slot["start_time"] == "14:10"  # Derived synchronously!
        assert resched_slot["end_time"] == "15:50"

        # Reminders check: at 07:50 (morning), NO reminder should trigger!
        rem_morning = get_upcoming_reminders(storage, ref_dt=datetime(2026, 9, 8, 7, 50), lookahead_minutes=30)
        assert len(rem_morning) == 0

        # At 13:55 (15 mins before 14:10), reminder MUST trigger!
        rem_afternoon = get_upcoming_reminders(storage, ref_dt=datetime(2026, 9, 8, 13, 55), lookahead_minutes=30)
        assert len(rem_afternoon) == 1
        assert rem_afternoon[0]["start_time"] == "14:10"

        storage.close()


# -----------------------------------------------------------------------------
# 10. Multi-Semester Course ID Isolation (R9)
# -----------------------------------------------------------------------------

def test_multi_semester_course_id_isolation():
    """
    R9: Same course name and teacher in two different semesters must generate
    different course IDs and NEVER overwrite each other.
    """
    c1 = parse_markdown_schedule_table(SAMPLE_SCHEDULE_MD, semester="2026-2027-1")
    c2 = parse_markdown_schedule_table(SAMPLE_SCHEDULE_MD, semester="2025-2026-2")

    c1_map = {c.name: c.id for c in c1}
    c2_map = {c.name: c.id for c in c2}

    for name in c1_map:
        assert c1_map[name] != c2_map[name], f"course {name} ID collision across semesters!"


# -----------------------------------------------------------------------------
# 11. Zero Delete Soft Delete Compliance (R10)
# -----------------------------------------------------------------------------

def test_zero_delete_soft_delete_compliance():
    """
    R10: Calling delete_course or delete_event soft-deletes the item (is_deleted=1)
    and retains the row in SQLite for auditability.
    """
    with tempfile.TemporaryDirectory() as td:
        storage = ScheduleStorage(Path(td) / "schedule.db")
        course = Course(id="crs_soft_del", name="测试课程", semester="2026-2027-1")
        storage.save_course(course)

        # Soft delete
        storage.delete_course("crs_soft_del")

        # Query via normal API returns 0 active courses
        active = storage.list_courses("2026-2027-1")
        assert len(active) == 0

        # Physical row STILL EXISTS in SQLite with is_deleted=1 (Zero Delete compliance)!
        with sqlite3.connect(str(storage.db_path)) as conn:
            row = conn.execute("SELECT id, is_deleted, deleted_at FROM courses WHERE id='crs_soft_del';").fetchone()
            assert row is not None
            assert row[1] == 1
            assert row[2] is not None  # Timestamp preserved for audit!

        storage.close()
