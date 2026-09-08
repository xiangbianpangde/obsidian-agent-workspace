"""
Unified IM Hub - Deterministic Focus & Attention Rules Engine (v0.2.8 / R2-R5)
Implements typed decisions, strict priority resolution, student chatter suppression,
and unhandled message registration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple


@dataclass(frozen=True)
class FocusDecision:
    action: Literal["MUST_PROCESS", "HIGHLIGHT", "REGISTER_TODO", "FOLD", "IGNORE"]
    priority: Literal["urgent", "high", "normal", "low", "none"]
    must_process: bool
    is_folded: bool
    tags: List[str]
    reasons: List[str]
    matched_rule_id: str


DEFAULT_COURSE_TEACHERS = [
    "田莎莎", "谭永荣", "李娜", "胡丽霞", "吴立锋",
    "谢金翠", "郝家春", "宫丽", "张翼", "姚欣雨", "高志荣", "康老师"
]

DEFAULT_COURSE_KEYWORDS = [
    "数字电子技术", "数电", "概率论", "数理统计", "深度学习", "学术英语",
    "数据结构", "大学物理", "大物", "体育", "毛泽东思想", "认识自我",
    "计算机学院", "人工2502"
]


def evaluate_focus_decision(
    channel_name: str,
    channel_type: str,
    is_focus: bool = False,
    text: str = "",
    message_type: str = "text",
    mentions: Optional[List[Dict[str, Any]]] = None,
    source: str = "wechat",
    sender_name: str = "",
    sender_id: Optional[str] = None,
    is_self: Optional[bool] = None,
    course_teachers: Optional[List[str]] = None,
    course_keywords: Optional[List[str]] = None,
) -> FocusDecision:
    """
    Evaluates multi-source attention rules against user's specific requirements.
    Returns a typed FocusDecision object.
    """
    teachers = course_teachers if course_teachers is not None else DEFAULT_COURSE_TEACHERS
    keywords = course_keywords if course_keywords is not None else DEFAULT_COURSE_KEYWORDS
    mentions_list = mentions or []

    # -------------------------------------------------------------------------
    # Rule 0: 领导康老师发信 (最高优先级，任何群聊/私聊无条件立即生效，绝不被吞)
    # -------------------------------------------------------------------------
    is_kang = (
        "康老师" in sender_name
        or sender_id == "44056960"
        or (channel_type == "direct" and "康老师" in channel_name)
    )
    if is_kang and is_self is not True:
        return FocusDecision(
            action="MUST_PROCESS",
            priority="urgent",
            must_process=True,
            is_folded=False,
            tags=["leader_urgent_todo"],
            reasons=["领导「康老师」消息 · 必须最高优先级逐条处理"],
            matched_rule_id="RULE_KANG_LEADER",
        )

    # -------------------------------------------------------------------------
    # 1. QQ Focus Rules
    # -------------------------------------------------------------------------
    if source == "qq":
        # 1.1 人工2502班通知群 (班级群，全量必看，必须单独点出)
        if channel_type == "group" and ("人工2502" in channel_name or "2502班" in channel_name):
            return FocusDecision(
                action="MUST_PROCESS",
                priority="high",
                must_process=True,
                is_folded=False,
                tags=["class_must_read"],
                reasons=["班级群「人工2502班通知群」消息 · 每条必看待办"],
                matched_rule_id="RULE_QQ_CLASS_2502",
            )

        # 1.2 乌鸦像写字台 (本人重要备忘，单独提出，待同步至 Obsidian)
        if "乌鸦像写字台" in channel_name or sender_name == "乌鸦像写字台":
            return FocusDecision(
                action="HIGHLIGHT",
                priority="high",
                must_process=False,
                is_folded=False,
                tags=["self_memo_obsidian"],
                reasons=["乌鸦像写字台 (本人重要备忘 · 待同步至 Obsidian)"],
                matched_rule_id="RULE_QQ_SELF_MEMO",
            )

        # 1.3 工作群: 2026新思路中高层群
        if channel_type == "group" and ("2026新思路" in channel_name or "新思路中高层" in channel_name or "新思路" in channel_name):
            return FocusDecision(
                action="HIGHLIGHT",
                priority="high",
                must_process=True,
                is_folded=False,
                tags=["work_group_focus"],
                reasons=["工作群「2026新思路中高层群」· 重点事项"],
                matched_rule_id="RULE_QQ_WORK_GROUP",
            )

        # 1.4 其他 QQ 私聊: 正常提醒看一眼，登记待处理
        if channel_type == "direct" and is_self is not True:
            return FocusDecision(
                action="REGISTER_TODO",
                priority="normal",
                must_process=True,
                is_folded=False,
                tags=["qq_direct_todo"],
                reasons=[f"QQ好友「{sender_name or channel_name}」消息 · 正常登记待处理"],
                matched_rule_id="RULE_QQ_DIRECT_TODO",
            )

        # 1.5 其他 QQ 群聊: 默认折叠 (除非发布了 @全体成员)
        if channel_type == "group":
            has_at_all = any(m.get("is_all") is True for m in mentions_list) or "@所有人" in text or "@全体成员" in text
            if has_at_all:
                return FocusDecision(
                    action="REGISTER_TODO",
                    priority="normal",
                    must_process=True,
                    is_folded=False,
                    tags=["mention_all"],
                    reasons=[f"群聊「{channel_name}」发布了 @全体成员"],
                    matched_rule_id="RULE_QQ_GROUP_AT_ALL",
                )
            return FocusDecision(
                action="FOLD",
                priority="none",
                must_process=False,
                is_folded=True,
                tags=[],
                reasons=[],
                matched_rule_id="RULE_QQ_GROUP_FOLDED",
            )

    # -------------------------------------------------------------------------
    # 2. 微信 (WeChat) Focus Rules: 所有未处理消息正常登记待处理
    # -------------------------------------------------------------------------
    elif source == "wechat":
        if is_self is not True:
            has_at_all = any(m.get("is_all") is True for m in mentions_list) or "@所有人" in text or "@全体成员" in text
            if has_at_all:
                return FocusDecision(
                    action="REGISTER_TODO",
                    priority="normal",
                    must_process=True,
                    is_folded=False,
                    tags=["mention_all"],
                    reasons=[f"微信群「{channel_name}」发布了 @全体成员"],
                    matched_rule_id="RULE_WECHAT_AT_ALL",
                )
            if channel_type == "direct":
                return FocusDecision(
                    action="REGISTER_TODO",
                    priority="normal",
                    must_process=True,
                    is_folded=False,
                    tags=["wechat_todo"],
                    reasons=[f"微信好友「{sender_name or channel_name}」私聊 · 正常登记待处理"],
                    matched_rule_id="RULE_WECHAT_DIRECT_TODO",
                )
            # 所有其他微信群聊消息：全量登记待处理
            return FocusDecision(
                action="REGISTER_TODO",
                priority="normal",
                must_process=True,
                is_folded=False,
                tags=["wechat_todo"],
                reasons=[f"微信群「{channel_name}」消息 · 正常登记待处理"],
                matched_rule_id="RULE_WECHAT_GROUP_TODO",
            )

    # -------------------------------------------------------------------------
    # 3. 企业微信 (WeCom) Focus Rules: 课程群仅老师，私聊全登记
    # -------------------------------------------------------------------------
    elif source == "wecom":
        # 3.1 所有个人信息 (私聊)，必须登记为待处理
        if channel_type == "direct" and is_self is not True:
            return FocusDecision(
                action="MUST_PROCESS",
                priority="high",
                must_process=True,
                is_folded=False,
                tags=["wecom_direct_todo"],
                reasons=[f"企业微信个人消息「{sender_name or channel_name}」· 必须登记待处理"],
                matched_rule_id="RULE_WECOM_DIRECT",
            )

        # 3.2 群聊关注逻辑：与课表匹配的课程群
        if channel_type == "group":
            is_course_group = any(kw in channel_name for kw in keywords) or "课" in channel_name or "大群" in channel_name
            if is_course_group:
                # 严格匹配任课教师，其他同学信息一律不登记（即使 student @本人 也被抑制）
                is_real_teacher = any(t == sender_name or (len(t) >= 2 and t in sender_name) for t in teachers)
                if not is_real_teacher:
                    is_real_teacher = any(title in sender_name for title in ["任课教师", "任课老师", "指导老师", "辅导员"])
                if is_real_teacher:
                    return FocusDecision(
                        action="MUST_PROCESS",
                        priority="high",
                        must_process=True,
                        is_folded=False,
                        tags=["course_teacher_notice"],
                        reasons=[f"课程群「{channel_name}」任课老师「{sender_name}」发布重要通知"],
                        matched_rule_id="RULE_WECOM_TEACHER",
                    )
                else:
                    # 学生闲聊或通知：坚决抑制，不打 focus 标签
                    return FocusDecision(
                        action="IGNORE",
                        priority="none",
                        must_process=False,
                        is_folded=False,
                        tags=[],
                        reasons=[],
                        matched_rule_id="RULE_WECOM_STUDENT_SUPPRESSED",
                    )
            else:
                has_at_all = "@所有人" in text or "@全体成员" in text or "@all" in text.lower()
                if has_at_all:
                    return FocusDecision(
                        action="REGISTER_TODO",
                        priority="normal",
                        must_process=True,
                        is_folded=False,
                        tags=["mention_all"],
                        reasons=[f"企微群「{channel_name}」发布了 @全体成员"],
                        matched_rule_id="RULE_WECOM_GROUP_AT_ALL",
                    )

    # 4. 通用 Mention Self 检测（前提是没有被前述规则明确 IGNORE / 抑制）
    if any(m.get("is_self") is True for m in mentions_list):
        return FocusDecision(
            action="MUST_PROCESS",
            priority="high",
            must_process=True,
            is_folded=False,
            tags=["mention_self"],
            reasons=[f"在「{channel_name}」中被直接提及 (@你)"],
            matched_rule_id="RULE_MENTION_SELF",
        )

    return FocusDecision(
        action="IGNORE",
        priority="none",
        must_process=False,
        is_folded=False,
        tags=[],
        reasons=[],
        matched_rule_id="RULE_DEFAULT_PASS",
    )


def evaluate_focus_rules(
    channel_name: str,
    channel_type: str,
    is_focus: bool = False,
    text: str = "",
    message_type: str = "text",
    mentions: Optional[List[Dict[str, Any]]] = None,
    source: str = "wechat",
    sender_name: str = "",
    sender_id: Optional[str] = None,
    is_self: Optional[bool] = None,
    course_teachers: Optional[List[str]] = None,
    course_keywords: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Backward-compatible wrapper returning (tags, reasons)."""
    decision = evaluate_focus_decision(
        channel_name=channel_name,
        channel_type=channel_type,
        is_focus=is_focus,
        text=text,
        message_type=message_type,
        mentions=mentions,
        source=source,
        sender_name=sender_name,
        sender_id=sender_id,
        is_self=is_self,
        course_teachers=course_teachers,
        course_keywords=course_keywords,
    )
    return decision.tags, decision.reasons
