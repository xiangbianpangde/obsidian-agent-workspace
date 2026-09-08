"""Schedule Parsers & Importers (v0.2.8 / R9 Multi-semester Isolation)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import Course, CourseTimeSlot, period_range_to_time

DAY_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7,
}

PALETTE = [
    "#3b82f6",  # Blue
    "#8b5cf6",  # Purple
    "#06b6d4",  # Cyan
    "#10b981",  # Emerald
    "#f59e0b",  # Amber
    "#ec4899",  # Pink
    "#f97316",  # Orange
    "#6366f1",  # Indigo
    "#14b8a6",  # Teal
    "#a855f7",  # Violet
    "#e11d48",  # Rose
]


def pick_course_color(course_name: str) -> str:
    h = int(hashlib.md5(course_name.encode("utf-8")).hexdigest()[:8], 16)
    return PALETTE[h % len(PALETTE)]


def parse_day_of_week(raw: str) -> int:
    s = raw.strip().lower()
    for k, v in DAY_MAP.items():
        if k in s:
            return v
    return 1


def parse_period_range(raw: str) -> Tuple[int, int]:
    nums = [int(n) for n in re.findall(r"\d+", raw)]
    if len(nums) == 1:
        return nums[0], nums[0]
    if len(nums) >= 2:
        return nums[0], nums[1]
    return 1, 2


def parse_week_pattern(raw: str) -> Tuple[int, int, str, List[int]]:
    pattern = "all"
    if "单" in raw:
        pattern = "odd"
    elif "双" in raw:
        pattern = "even"

    nums = [int(n) for n in re.findall(r"\d+", raw)]
    if len(nums) == 1:
        return nums[0], nums[0], pattern, [nums[0]]
    if len(nums) >= 2:
        return nums[0], nums[1], pattern, []
    return 1, 16, pattern, []


def parse_markdown_schedule_table(markdown_text: str, semester: str = "2026-2027-1") -> List[Course]:
    """
    Parses a standard markdown course table into structured Course objects.
    Enforces multi-semester isolation: course IDs incorporate `semester`
    so different semesters never collide or overwrite each other.
    """
    lines = markdown_text.splitlines()
    in_table = False
    raw_rows: List[Dict[str, str]] = []

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        if "星期" in line_clean and "节次" in line_clean and "课程" in line_clean:
            in_table = True
            continue
        if in_table:
            if line_clean.startswith("##"):
                in_table = False
                continue
            if not line_clean.startswith("|"):
                continue
            cols = [c.strip() for c in line_clean.split("|")[1:-1]]
            if len(cols) >= 6 and not cols[0].startswith("---"):
                raw_rows.append({
                    "day": cols[0],
                    "period": cols[1],
                    "time": cols[2] if len(cols) >= 8 else "",
                    "name": cols[3] if len(cols) >= 8 else cols[2],
                    "code": cols[4].strip("`") if len(cols) >= 8 else "",
                    "weeks": cols[5] if len(cols) >= 8 else cols[3],
                    "teacher": cols[6] if len(cols) >= 8 else cols[4],
                    "classroom": cols[7] if len(cols) >= 8 else (cols[5] if len(cols) >= 6 else ""),
                })

    # Group by semester + course name + teacher
    course_map: Dict[str, Course] = {}

    for row in raw_rows:
        name = row["name"]
        if not name:
            continue
        # Multi-semester isolation: key includes semester!
        course_key = f"{semester}::{name}::{row.get('teacher', '')}"

        if course_key not in course_map:
            sem_clean = semester.replace("-", "_")
            cid = f"crs_{sem_clean}_{hashlib.md5(course_key.encode()).hexdigest()[:10]}"
            course_map[course_key] = Course(
                id=cid,
                name=name,
                code=row.get("code", ""),
                teacher=row.get("teacher", ""),
                classroom=row.get("classroom", ""),
                semester=semester,
                color=pick_course_color(name),
                time_slots=[],
            )

        course = course_map[course_key]
        day = parse_day_of_week(row["day"])
        start_p, end_p = parse_period_range(row["period"])
        start_t, end_t = period_range_to_time(start_p, end_p)
        start_w, end_w, pattern, custom_w = parse_week_pattern(row["weeks"])
        room = row.get("classroom") or course.classroom

        slot_key = f"{course.id}::{day}::{start_p}_{end_p}::{pattern}::{start_w}_{end_w}"
        slot_id = f"ts_{hashlib.md5(slot_key.encode()).hexdigest()[:12]}"

        slot = CourseTimeSlot(
            id=slot_id,
            course_id=course.id,
            day_of_week=day,
            start_period=start_p,
            end_period=end_p,
            start_time=start_t,
            end_time=end_t,
            week_pattern=pattern,
            start_week=start_w,
            end_week=end_w,
            custom_weeks=custom_w,
            classroom=room,
        )
        course.time_slots.append(slot)

    return list(course_map.values())


def import_schedule_from_obsidian_vault(
    vault_path: Path,
    semester: str = "2026-2027-1",
    relative_path: str = "07.学习笔记/大二上/课表.md"
) -> List[Course]:
    """
    Finds and parses 课表.md inside the user's Obsidian Vault.
    Prefers the canonical `07.学习笔记/大二上/课表.md` path.
    """
    target = vault_path / relative_path
    if not target.is_file():
        candidates = list(vault_path.glob("**/课表.md"))
        if not candidates:
            raise FileNotFoundError(f"未在 Obsidian 知识库中找到课表文件 ({relative_path})")
        target = candidates[0]

    content = target.read_text(encoding="utf-8")
    return parse_markdown_schedule_table(content, semester=semester)
