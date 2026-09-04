"""
Unified IM Hub - Deterministic Focus Rules for Students
Conforms strictly to docs/03-im-integration-v0.2.7.md
Implements explainable, transparent, multi-rule accumulation for academic notifications.
"""

from typing import Any, Dict, List, Tuple


def evaluate_focus_rules(
    channel_name: str,
    channel_type: str,
    is_focus: bool,
    text: str,
    message_type: str,
    mentions: List[Dict[str, Any]]
) -> Tuple[List[str], List[str]]:
    """
    Evaluates deterministic focus rules against message facts and channel context.
    Returns (focus_tags, focus_reasons).
    Supports multi-rule accumulation (e.g. school notice + @all + @self).
    """
    tags: List[str] = []
    reasons: List[str] = []

    is_academic_channel = is_focus or any(kw in channel_name for kw in ["通知", "班", "学院", "教务", "课程", "科研", "大作业"])

    # 1. Academic & Course Group Rules
    if is_academic_channel:
        # Check @all
        has_at_all = any(m.get("is_all") is True for m in (mentions or []))
        if has_at_all:
            tags.append("mention_all")
            reasons.append(f"学校/班级群「{channel_name}」发布了 @全体成员")

        # Check notice keywords or notice message_type
        has_notice = (message_type == "notice") or any(kw in text for kw in ["通知", "提醒", "截止", "作业", "考试", "放假", "调课", "请注意"])
        if has_notice:
            tags.append("school")
            reasons.append(f"学校/班级群「{channel_name}」发布了重要通知")

    # 2. Mention Self Rule
    has_at_self = any(m.get("is_self") is True for m in (mentions or []))
    if has_at_self:
        tags.append("mention_self")
        reasons.append(f"在「{channel_name}」中被提及 (@你)")

    # 3. Important Direct Messages
    if channel_type == "direct" and is_focus:
        tags.append("direct_important")
        reasons.append(f"重要联系人「{channel_name}」发送了私聊")

    return tags, reasons
