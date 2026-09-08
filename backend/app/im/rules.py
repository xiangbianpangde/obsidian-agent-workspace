"""
Unified IM Hub - Deterministic Focus & Attention Rules Engine
Conforms strictly to user's academic focus specifications:
- QQ:
  1. 人工2502班通知群 (班级群 · 全量必看高优待办)
  2. 乌鸦像写字台 (个人重要备忘 · 待同步至 Obsidian)
  3. 2026新思路中高层群 (工作群 · 重点关注)
  4. 康老师 (领导 · 逐条必处理)
  5. 其他群默认折叠；其他私聊正常登记待处理
- 微信:
  所有未处理消息正常登记待处理
- 企业微信:
  与课表/课程联动：课程群内仅关注任课老师消息，其他同学信息不登记；所有私聊登记为待处理
"""

from typing import Any, Dict, List, Optional, Tuple

DEFAULT_COURSE_TEACHERS = [
    "田莎莎", "谭永荣", "李娜", "胡丽霞", "吴立锋",
    "谢金翠", "郝家春", "宫丽", "张翼", "姚欣雨", "高志荣", "康老师"
]

DEFAULT_COURSE_KEYWORDS = [
    "数字电子技术", "数电", "概率论", "数理统计", "深度学习", "学术英语",
    "数据结构", "大学物理", "大物", "体育", "毛泽东思想", "认识自我",
    "计算机学院", "人工2502"
]


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
    """
    Evaluates multi-source attention rules against user's specific requirements.
    Returns (focus_tags, focus_reasons).
    """
    tags: List[str] = []
    reasons: List[str] = []

    teachers = course_teachers or DEFAULT_COURSE_TEACHERS
    keywords = course_keywords or DEFAULT_COURSE_KEYWORDS

    # -------------------------------------------------------------------------
    # 1. QQ Focus Rules
    # -------------------------------------------------------------------------
    if source == "qq":
        # 1.1 人工2502班通知群 (班级群，全量必看，必须单独点出)
        if "人工2502" in channel_name or "人工2502班通知群" in channel_name:
            tags.append("class_must_read")
            reasons.append("班级群「人工2502班通知群」· 全量必看高优待办")

        # 1.2 乌鸦像写字台 (本人重要备忘，单独提出，后续 AI 自动入库 Obsidian)
        elif "乌鸦像写字台" in channel_name or sender_name == "乌鸦像写字台":
            tags.append("self_memo_obsidian")
            reasons.append("乌鸦像写字台 (本人重要备忘 · 待同步至 Obsidian)")

        # 1.3 领导重点关注: 康老师 (必须重点关注，每条信息都必须处理)
        elif "康老师" in sender_name or "康老师" in channel_name:
            tags.append("leader_urgent_todo")
            reasons.append("领导「康老师」消息 · 必须重点逐条处理")

        # 1.4 工作群: 2026新思路中高层群
        elif "2026新思路" in channel_name or "新思路中高层" in channel_name or "新思路" in channel_name:
            tags.append("work_group_focus")
            reasons.append("工作群「2026新思路中高层群」· 重点事项")

        # 1.5 其他私聊: 正常提醒看一眼，登记待处理
        elif channel_type == "direct":
            tags.append("qq_direct_todo")
            reasons.append(f"QQ好友「{sender_name or channel_name}」消息 · 正常登记待处理")

        # 1.6 其他群聊: 默认折叠 (除非有 @全体成员)
        else:
            has_at_all = any(m.get("is_all") is True for m in (mentions or []))
            if has_at_all or "@所有人" in text or "@全体成员" in text:
                tags.append("mention_all")
                reasons.append(f"群聊「{channel_name}」发布了 @全体成员")
            else:
                tags.append("folded_group")

    # -------------------------------------------------------------------------
    # 2. 微信 (WeChat) Focus Rules
    # -------------------------------------------------------------------------
    elif source == "wechat":
        # 所有未处理信息正常登记待处理
        if is_self is not True:
            has_at_all = any(m.get("is_all") is True for m in (mentions or [])) or "@所有人" in text or "@全体成员" in text
            if has_at_all:
                tags.append("mention_all")
                reasons.append(f"微信群「{channel_name}」发布了 @全体成员")
            elif channel_type == "direct":
                tags.append("wechat_todo")
                reasons.append(f"微信好友「{sender_name or channel_name}」私聊 · 正常登记待处理")
            elif any(kw in channel_name for kw in ["通知", "班", "学院", "教务", "课程", "科研"]):
                tags.append("wechat_todo")
                reasons.append(f"微信群「{channel_name}」消息 · 正常登记待处理")

    # -------------------------------------------------------------------------
    # 3. 企业微信 (WeCom) Focus Rules
    # -------------------------------------------------------------------------
    elif source == "wecom":
        # 3.1 所有个人信息 (私聊)，必须登记为待处理
        if channel_type == "direct" and is_self is not True:
            tags.append("wecom_direct_todo")
            reasons.append(f"企业微信联系人「{sender_name or channel_name}」私聊 · 必须登记待处理")

        # 3.2 群聊关注逻辑：与课表匹配的课程群
        elif channel_type == "group":
            is_course_group = any(kw in channel_name for kw in keywords) or "课" in channel_name or "大群" in channel_name
            if is_course_group:
                # 仅关注任课老师的信息，其他学生信息一律不登记！
                is_teacher_sender = (
                    any(t in sender_name for t in teachers)
                    or "老师" in sender_name
                    or "任课" in sender_name
                    or "教务" in sender_name
                    or "辅导员" in sender_name
                )
                if is_teacher_sender:
                    tags.append("course_teacher_notice")
                    reasons.append(f"课程群「{channel_name}」任课老师「{sender_name}」发布重要通知")
                else:
                    # 其他同学/人员信息，一律不登记为 focus
                    pass
            else:
                # 非课程群有 @全体成员 时作为普通通知提醒
                if "@所有人" in text or "@全体成员" in text or "@all" in text.lower():
                    tags.append("mention_all")
                    reasons.append(f"企微群「{channel_name}」发布了 @全体成员")

    # 通用 Mention Self 检测
    if any(m.get("is_self") is True for m in (mentions or [])):
        if "mention_self" not in tags:
            tags.append("mention_self")
            reasons.append(f"在「{channel_name}」中被直接提及 (@你)")

    return tags, reasons
