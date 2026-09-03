"""Template Compatibility Layer (v0.2 §5 / M4 rev2).
P1-M4-1: 两阶段渲染与完整 target_path 上下文。
P1-M4-3: Fail-closed 支持等级判定，动态/未知表达式降级而不猜测执行。
合同收口: 支持 custom vars (tp.user.* / {{key}})。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# 纯 JS 块: <%* ... %>
_RE_PURE_JS_BLOCK = re.compile(r"<%\*\s*(.*?)\s*%>", re.DOTALL)

# 文件移动指令: <% await tp.file.move(...) %>
_RE_FILE_MOVE = re.compile(
    r"""<%\s*(?:await\s+)?tp\.file\.move\s*\(\s*["']([^"']+)["']\s*(?:\+\s*tp\.file\.title)?\s*\)\s*%>""",
    re.IGNORECASE,
)

# 日期: <% tp.date.now(...) %>
_RE_DATE_NOW = re.compile(r"""<%\s*tp\.date\.now\((.*?)\)\s*%>""")

# 文件元信息
_RE_FILE_TITLE = re.compile(r"""<%\s*tp\.file\.title\s*%>""")
_RE_FILE_PATH = re.compile(r"""<%\s*tp\.file\.path\s*%>""")

# 用户自定义变量: <% tp.user.var_name %> 或 {{var_name}}
_RE_USER_VAR_TP = re.compile(r"""<%\s*tp\.user\.([A-Za-z0-9_-]+)\s*%>""")
_RE_USER_VAR_MUSTACHE = re.compile(r"""\{\{\s*([A-Za-z0-9_-]+)\s*\}\}""")

# 通用 Templater 标记（用于检测未受支持的表达式）
_RE_ANY_TAG = re.compile(r"""<%([^%]+)%>""")


def _format_date(dt: datetime, fmt: str) -> str:
    py_fmt = (
        fmt.replace("YYYY", "%Y")
        .replace("MM", "%m")
        .replace("DD", "%d")
        .replace("HH", "%H")
        .replace("mm", "%M")
        .replace("ss", "%S")
    )
    return dt.strftime(py_fmt)


def _is_quoted_literal(s: str) -> bool:
    s = s.strip()
    return (s.startswith('"') and s.endswith('"') and len(s) >= 2) or (
        s.startswith("'") and s.endswith("'") and len(s) >= 2
    )


def _eval_date_now(args_str: str, base_dt: datetime) -> tuple[bool, str]:
    """
    解析 tp.date.now 参数。
    P1-M4-3: 严格 fail-closed。
    - 参数数量 > 3: 拒绝 (False, "")
    - 第一参数必须为字面量字符串（带双引号或单引号），若为动态变量如 fmt，拒绝
    - 第二参数必须为合法整数字面量
    - 第三参数必须为字面量日期（YYYY-MM-DD）
    """
    args = [a.strip() for a in args_str.split(",") if a.strip()]
    if len(args) > 3:
        return False, ""

    fmt = "YYYY-MM-DD"
    offset_days = 0
    ref_date = base_dt

    if len(args) >= 1:
        if not _is_quoted_literal(args[0]):
            return False, ""  # 动态 format 如 fmt，fail-closed
        fmt = args[0][1:-1]
    if len(args) >= 2:
        val_str = args[1].strip("\"' ")
        try:
            offset_days = int(val_str)
        except ValueError:
            return False, ""
    if len(args) >= 3:
        if not _is_quoted_literal(args[2]):
            return False, ""
        ref_str = args[2][1:-1]
        try:
            ref_date = datetime.strptime(ref_str, "%Y-%m-%d")
        except Exception:
            return False, ""

    try:
        target_dt = ref_date + timedelta(days=offset_days)
        return True, _format_date(target_dt, fmt)
    except Exception:
        return False, ""


def inspect_template(raw: str) -> dict[str, Any]:
    """分析模板语法特性与降级级别（P1-M4-3: 严格 fail-closed）。"""
    has_js = bool(_RE_PURE_JS_BLOCK.search(raw))
    move_match = _RE_FILE_MOVE.search(raw)
    suggested_dir = None
    if move_match:
        suggested_dir = move_match.group(1).strip().strip("/")

    has_date = bool(_RE_DATE_NOW.search(raw))
    has_title = bool(_RE_FILE_TITLE.search(raw))
    has_path = bool(_RE_FILE_PATH.search(raw))

    # 去掉纯 JS 块和已知的 move 语句后，检查是否有未识别的 Templater 表达式
    cleaned_for_check = _RE_PURE_JS_BLOCK.sub("", raw)
    cleaned_for_check = _RE_FILE_MOVE.sub("", cleaned_for_check)
    cleaned_for_check = _RE_FILE_TITLE.sub("", cleaned_for_check)
    cleaned_for_check = _RE_FILE_PATH.sub("", cleaned_for_check)
    cleaned_for_check = _RE_USER_VAR_TP.sub("", cleaned_for_check)

    has_unsupported = False
    for m in _RE_ANY_TAG.finditer(cleaned_for_check):
        content = m.group(1).strip()
        # 检查是否为合法的 tp.date.now 常量表达式
        date_match = re.match(r"^tp\.date\.now\((.*)\)$", content)
        if date_match:
            ok, _ = _eval_date_now(date_match.group(1), datetime.now())
            if not ok:
                has_unsupported = True
                break
        else:
            has_unsupported = True
            break

    level = "degraded" if (has_js or has_unsupported) else "full"

    return {
        "has_js_block": has_js,
        "has_unsupported_tags": has_unsupported,
        "has_date": has_date,
        "has_title": has_title,
        "has_file_path": has_path,
        "has_file_move": bool(move_match),
        "suggested_dir": suggested_dir,
        "supported_level": level,
    }


def compute_target_path(
    clean_title: str,
    suggested_dir: str | None,
    custom_path: str | None = None,
) -> str:
    """计算目标文件最终相对路径。"""
    if custom_path and custom_path.strip():
        target = custom_path.strip()
    elif suggested_dir:
        target = f"{suggested_dir}/{clean_title}"
    else:
        target = clean_title
    if not target.endswith(".md"):
        target += ".md"
    return target


def render_template(
    raw: str,
    *,
    title: str,
    target_path: str = "",
    vars: dict[str, str] | None = None,
    now_dt: datetime | None = None,
) -> tuple[str, str | None]:
    """
    两阶段渲染（P1-M4-1）：
    调用前需提供计算好的 target_path。
    - 替换 tp.file.title
    - 替换 tp.file.path 为 target_path
    - 替换 custom vars (<% tp.user.k %> 与 {{k}})
    - 替换合法常量的 tp.date.now(...)，非法/动态的保持原样（fail-closed）
    - 移除 tp.file.move 移动语句
    - 对 <%* ... %> 插入降级标记并原样保留
    返回: (rendered_content, suggested_dir)
    """
    if now_dt is None:
        now_dt = datetime.now()
    if vars is None:
        vars = {}

    info = inspect_template(raw)
    suggested_dir = info["suggested_dir"]

    # 1. 移除 tp.file.move 行
    lines = []
    for line in raw.splitlines():
        if _RE_FILE_MOVE.search(line):
            continue
        lines.append(line)
    result = "\n".join(lines)

    # 2. 替换 tp.file.title
    result = _RE_FILE_TITLE.sub(title, result)

    # 3. 替换 tp.file.path (P1-M4-1)
    result = _RE_FILE_PATH.sub(target_path, result)

    # 4. 替换 custom vars
    for k, v in vars.items():
        escaped_k = re.escape(k)
        result = re.sub(rf"<%\s*tp\.user\.{escaped_k}\s*%>", str(v), result)
        result = re.sub(rf"\{{\{{\s*{escaped_k}\s*\}}\}}", str(v), result)

    # 5. 替换 tp.date.now (P1-M4-3: fail-closed)
    def _replace_date(match: re.Match) -> str:
        args_str = match.group(1).strip()
        ok, formatted = _eval_date_now(args_str, now_dt)
        if ok:
            return formatted
        # 无法求值的动态参数，保留原表达式
        return match.group(0)

    result = _RE_DATE_NOW.sub(_replace_date, result)

    # 6. 对纯 JS 块插入降级说明
    def _wrap_js_block(match: re.Match) -> str:
        js_code = match.group(0)
        warning = "\n<!-- workspace: unsupported Templater JS block (will execute in Obsidian) -->\n"
        return warning + js_code + "\n"

    result = _RE_PURE_JS_BLOCK.sub(_wrap_js_block, result)

    return result, suggested_dir
