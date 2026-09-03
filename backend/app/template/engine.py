"""Template Compatibility Layer (v0.2 §5 / M4).
支持 Templater 子集：
- tp.date.now(format?, offset?, reference?)
- tp.file.title / tp.file.path
- tp.file.move -> 转换为 target_dir 建议，并从生成文本中移除
- <%* ... %> -> JS 块降级保护
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# 正则表达式集合
_RE_JS_BLOCK = re.compile(r"<%[*]?(.*?)(?:tR\s*\+=.*?)?%>", re.DOTALL)
_RE_PURE_JS_BLOCK = re.compile(r"<%\*\s*(.*?)\s*%>", re.DOTALL)
_RE_FILE_MOVE = re.compile(
    r"""<%\s*(?:await\s+)?tp\.file\.move\s*\(\s*["']([^"']+)["']\s*(?:\+\s*tp\.file\.title)?\s*\)\s*%>""",
    re.IGNORECASE,
)
_RE_DATE_NOW = re.compile(r"""<%\s*tp\.date\.now\((.*?)\)\s*%>""")
_RE_FILE_TITLE = re.compile(r"""<%\s*tp\.file\.title\s*%>""")
_RE_FILE_PATH = re.compile(r"""<%\s*tp\.file\.path\s*%>""")


def _format_date(dt: datetime, fmt: str) -> str:
    # 简单的 Moment.js -> Python strftime 映射
    py_fmt = (
        fmt.replace("YYYY", "%Y")
        .replace("MM", "%m")
        .replace("DD", "%d")
        .replace("HH", "%H")
        .replace("mm", "%M")
        .replace("ss", "%S")
    )
    return dt.strftime(py_fmt)


def _eval_date_now(args_str: str, base_dt: datetime) -> str:
    args = [a.strip() for a in args_str.split(",") if a.strip()]
    fmt = "YYYY-MM-DD"
    offset_days = 0
    ref_date = base_dt

    if len(args) >= 1:
        # 第一个参数是 format 字符串
        fmt = args[0].strip("\"' ")
    if len(args) >= 2:
        # 第二个参数是 offset（天数，例如 -1, 1）
        try:
            offset_days = int(args[1].strip())
        except ValueError:
            offset_days = 0
    if len(args) >= 3:
        # 第三个参数是参考日期字符串
        ref_str = args[2].strip("\"' ")
        try:
            ref_date = datetime.strptime(ref_str, "%Y-%m-%d")
        except Exception:
            ref_date = base_dt

    target_dt = ref_date + timedelta(days=offset_days)
    return _format_date(target_dt, fmt)


def inspect_template(raw: str) -> dict[str, Any]:
    """分析模板语法特性与降级级别。"""
    has_js = bool(_RE_PURE_JS_BLOCK.search(raw))
    move_match = _RE_FILE_MOVE.search(raw)
    suggested_dir = None
    if move_match:
        raw_dir = move_match.group(1).strip()
        # 清理例如 /01. 采集 Grasp/所有采集/
        suggested_dir = raw_dir.strip("/")

    has_date = "tp.date.now" in raw
    has_title = "tp.file.title" in raw

    level = "degraded" if has_js else "full"

    return {
        "has_js_block": has_js,
        "has_date": has_date,
        "has_title": has_title,
        "has_file_move": bool(move_match),
        "suggested_dir": suggested_dir,
        "supported_level": level,
    }


def render_template(
    raw: str,
    *,
    title: str,
    target_path: str = "",
    now_dt: datetime | None = None,
) -> tuple[str, str | None]:
    """
    渲染模板：
    - 替换 tp.date.now(...)
    - 替换 tp.file.title
    - 替换 tp.file.path
    - 提取并移除 tp.file.move 语句
    - 对 <%* ... %> 插入降级标记并保留原逻辑
    返回: (rendered_content, suggested_dir)
    """
    if now_dt is None:
        now_dt = datetime.now()

    info = inspect_template(raw)
    suggested_dir = info["suggested_dir"]

    # 1. 移除 tp.file.move 这一整行（因为创建时已经直接在目标目录落位）
    def _strip_file_move(text: str) -> str:
        lines = []
        for line in text.splitlines():
            if _RE_FILE_MOVE.search(line):
                continue
            lines.append(line)
        return "\n".join(lines)

    result = _strip_file_move(raw)

    # 2. 替换 tp.file.title
    result = _RE_FILE_TITLE.sub(title, result)

    # 3. 替换 tp.file.path
    result = _RE_FILE_PATH.sub(target_path, result)

    # 4. 替换 tp.date.now(fmt, offset, ref)
    def _replace_date(match: re.Match) -> str:
        args_str = match.group(1).strip()
        return _eval_date_now(args_str, now_dt)

    result = _RE_DATE_NOW.sub(_replace_date, result)

    # 5. 对 JS 块插入降级保护提示（保留原代码块）
    def _wrap_js_block(match: re.Match) -> str:
        js_code = match.group(0)
        warning = "\n<!-- workspace: unsupported Templater JS block (will execute in Obsidian) -->\n"
        return warning + js_code + "\n"

    result = _RE_PURE_JS_BLOCK.sub(_wrap_js_block, result)

    return result, suggested_dir
