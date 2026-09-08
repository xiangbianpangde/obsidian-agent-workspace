"""Comprehensive Test Suite for Schedule, Academic Calendar & Focus Rules."""

import json
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

    # 6.5 QQ: 普通群聊 (默认折叠)
    tags, reasons = evaluate_focus_rules(
        channel_name="王者荣耀开黑群",
        channel_type="group",
        source="qq",
        sender_name="张三",
        text="今晚来一把"
    )
    assert "folded_group" in tags

    # 6.6 微信: 未处理消息正常登记待办
    tags, reasons = evaluate_focus_rules(
        channel_name="张同学",
        channel_type="direct",
        source="wechat",
        sender_name="张同学",
        text="明天一起去图书馆吗"
    )
    assert "wechat_todo" in tags

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

    # Case B: 同学发言 -> 不登记！
    tags_s, reasons_s = evaluate_focus_rules(
        channel_name="大学物理B(2)-2026-2027-1",
        channel_type="group",
        source="wecom",
        sender_name="李同学",
        text="收到老师"
    )
    assert "course_teacher_notice" not in tags_s
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

    # 6. DELETE event
    r7 = client.delete(f"/api/schedule/event/{eid}")
    assert r7.status_code == 200
