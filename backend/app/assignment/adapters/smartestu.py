"""数你最灵 (smartestu.cn) read-only assignment adapter.

认证：Bearer JWT（浏览器 DevTools 复制），自动尝试 POST /api/auth/refresh 续期一次；
refresh 失败即标记凭证失效，绝不进入刷新风暴。
查询链路（端点已从前端 bundle 静态分析确认）：
  POST /api/homework/student/portal-summary   作业门户汇总
  POST /api/homework/student/course/query     课程列表
  POST /api/homework/student/mark/queryHomeworks  作业列表
响应字段未在官方文档公开，提取逻辑全部容错（多候选字段名 + 递归扫描），首联后按真实
流量微调即可，解析失败不 crash（fail-closed → status=unknown）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from ..http_client import AdapterError, BasePlatformClient, CredentialInvalidError
from ..models import NormalizedTask, PlatformName, TaskStatus
from .chaoxing import parse_deadline_text
from .cdp_channel import fetch_via_cdp

logger = logging.getLogger(__name__)

BASE_URL = "https://smartestu.cn"
PORTAL_SUMMARY_URL = BASE_URL + "/api/homework/student/portal-summary"
COURSE_QUERY_URL = BASE_URL + "/api/homework/student/course/query"
QUERY_HOMEWORKS_URL = BASE_URL + "/api/homework/student/mark/queryHomeworks"
REFRESH_URL = BASE_URL + "/api/auth/refresh"

_TITLE_KEYS = ("title", "homeworkName", "name", "homeworkTitle", "assignmentName")
_COURSE_NAME_KEYS = ("courseName", "courseTitle", "course", "className")
_ID_KEYS = ("homeworkId", "id", "hwId", "assignmentId", "workId")
_DUE_KEYS = (
    "deadline",
    "dueTime",
    "dueAt",
    "dueDate",
    "endTime",
    "submitDeadline",
    "homeworkDeadline",
    "deadlineTime",
)
_STATUS_KEYS = ("status", "submitStatus", "homeworkStatus", "state")
_SCORE_KEYS = ("score", "grade", "finalScore", "totalScore")

_STATUS_SUBMITTED = {"submitted", "done", "finished", "completed", "2", "1_submitted", "SUBMITTED"}
_STATUS_UNSUBMITTED = {"unsubmitted", "not_submitted", "todo", "pending", "0", "UNSUBMITTED"}
_STATUS_GRADED = {"graded", "corrected", "reviewed", "3", "GRADED"}


def _find_first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _normalize_status(raw: Any) -> TaskStatus:
    if raw is None:
        return "unknown"
    if isinstance(raw, bool):
        return "submitted" if raw else "unsubmitted"
    if isinstance(raw, (int, float)):
        raw = str(int(raw)) if float(raw).is_integer() else str(raw)
    s = str(raw).strip()
    sl = s.lower()
    if sl in _STATUS_GRADED:
        return "graded"
    if sl in _STATUS_SUBMITTED:
        return "submitted"
    if sl in _STATUS_UNSUBMITTED:
        return "unsubmitted"
    # 中文枚举兜底
    if any(w in s for w in ("已批改", "已评", "已打分")):
        return "graded"
    if any(w in s for w in ("已交", "已提交", "已完成")):
        return "submitted"
    if any(w in s for w in ("未交", "未提交", "待完成", "进行中")):
        return "unsubmitted"
    return "unknown"


def normalize_homework_item(
    item: dict[str, Any], course_hint: str = "", course_id: Any = None
) -> NormalizedTask | None:
    """Maps one studentCourseHomeworkDTO (real API shape) to NormalizedTask.

    实测字段：id/name/endTime(UTC ISO)/submission_status("not_submitted"|...)/
    review_status("not_reviewed"|"reviewed")/score。courseId 与课程名在外层分组，
    由调用方传入。
    """
    if not isinstance(item, dict):
        return None
    hw_id = item.get("id")
    title = item.get("name")
    if hw_id is None or not title:
        return None
    course_name = item.get("courseName") or course_hint or ""
    submission = str(item.get("submission_status") or "").lower()
    review = str(item.get("review_status") or "").lower()
    if review == "reviewed":
        status: TaskStatus = "graded"
    elif submission in ("", "not_submitted"):
        status = "unsubmitted"
    else:  # submitted / resubmitted 等
        status = "submitted"
    score_raw = item.get("score")
    try:
        score = float(score_raw) if score_raw is not None and status == "graded" else None
    except (TypeError, ValueError):
        score = None
    cid = item.get("courseId", course_id)
    external_id = f"hw-{cid}-{hw_id}" if cid is not None else f"hw-{hw_id}"
    return NormalizedTask(
        platform="smartestu",
        external_id=external_id,
        course_name=str(course_name),
        title=str(title),
        due_at=parse_deadline_text(item.get("endTime")),
        status=status,
        score=score,
        detail_url=BASE_URL + "/assignment",
        raw=item,
    )


def iter_homework_arrays(payload: Any) -> list[dict[str, Any]]:
    """Recursively collects dict items from list-valued fields of a response."""
    items: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(node, list):
            for el in node:
                if isinstance(el, dict):
                    items.append(el)
                    walk(el, depth + 1)
                elif isinstance(el, list):
                    walk(el, depth + 1)
        elif isinstance(node, dict):
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v, depth + 1)

    walk(payload)
    return items


class SmartestuAdapter:
    platform: PlatformName = "smartestu"

    def __init__(self, token: str, client: BasePlatformClient | None = None):
        raw = token.strip()
        # 三种凭证模式：
        #   "cookies:<jar>"  —— cookie 快照 + CDP 通道（推荐，2026-09 新协议唯一稳定路径）
        #   "refresh:<jwt>"  —— 旧 refreshToken 轮换（新协议下已废弃，导入即报协议升级）
        #   "<jwt>"          —— 短效 access token（DevTools 直拷，数小时有效）
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if raw.startswith("cookies:"):
            self._mode = "cdp"
            self._cookie_jar = raw[len("cookies:") :].strip()
            self._refresh_token = ""
            self._token = ""
        elif raw.startswith("refresh:"):
            raise CredentialInvalidError(
                "数你最灵登录协议已升级（__Host-auth_* cookie）：refreshToken 模式不可用，"
                "请重新从浏览器提取完整 cookie 快照（cookies: 前缀）"
            )
        else:
            self._mode = "token"
            self._cookie_jar = ""
            self._refresh_token = ""
            self._token = raw
        payload = self._cookie_jar or self._token
        if self._mode == "token" and (not payload or payload.count(".") < 2):
            raise CredentialInvalidError(
                "凭证无效：应为 cookies:<jar> 快照或 Bearer access token"
            )
        self.rotated_refresh_token: str | None = None
        # 轮换即时落盘回调：CDP 模式下页面每次握手都会轮换 cookie 快照
        self.on_rotate = None
        self._client = client or BasePlatformClient(
            headers={
                "Content-Type": "application/json",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/assignment",
            }
        )
        if self._token:
            self._client.set_auth_header(self._token)

    def _capture_rotation(self, new_refresh: str) -> None:
        self.rotated_refresh_token = new_refresh
        self._refresh_token = new_refresh
        if self.on_rotate:
            with contextlib.suppress(Exception):
                self.on_rotate("refresh:" + new_refresh)

    def _capture_jar_rotation(self, new_jar: str) -> None:
        self.rotated_cookie_jar = new_jar
        if self.on_rotate:
            with contextlib.suppress(Exception):
                self.on_rotate("cookies:" + new_jar)

    # ------------------------------------------------------------ public API
    def ensure_access_token(self) -> None:
        """token 模式下已有 Bearer token 则无需动作。

        历史：曾支持 "refresh:<jwt>" 模式在此用 refreshToken 换 access token。
        平台升级为 __Host-auth_* cookie 协议后该模式已废弃（构造时即拒绝），
        cdp / token 两种模式均持有可直接使用的凭证，故此处无操作。
        """
        if self._token:
            return
        resp = self._client.post(
            REFRESH_URL,
            content="",
            headers={"Cookie": f"refreshToken={self._refresh_token}"},
        )
        if resp.status_code != 200:
            raise CredentialInvalidError(
                "数你最灵 refreshToken 已失效（可能被其他端轮换），请重新登录并重新导入"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise AdapterError("refresh response is not json") from exc
        new_token = data.get("token") or ""
        if not new_token:
            raise AdapterError("refresh response has no token field")
        self._token = new_token
        self._client.set_auth_header(new_token)
        set_cookie = resp.headers.get("set-cookie", "")
        m = re.search(r"refreshToken=([^;\s]+)", set_cookie)
        if m and m.group(1) != self._refresh_token:
            self._capture_rotation(m.group(1))
        logger.info("smartestu access token refreshed via refreshToken")

    def validate(self) -> bool:
        if self._mode == "cdp":
            # CDP 通道：course/query 走通即视为凭证有效
            _courses, _groups, new_jar = fetch_via_cdp(self._cookie_jar)
            if new_jar != self._cookie_jar:
                self._capture_jar_rotation(new_jar)
            return True
        self.ensure_access_token()
        resp = self._client.post(PORTAL_SUMMARY_URL, content="{}")
        if resp.status_code == 401 and self._mode == "token" and self._try_refresh():
            # access token 过期：清内存值后 refresh 一次并重放
            resp = self._client.post(PORTAL_SUMMARY_URL, content="{}")
        if resp.status_code == 401:
            raise CredentialInvalidError(
                "数你最灵 token 已失效且刷新失败，请重新登录获取"
            )
        return resp.status_code == 200

    def fetch_tasks(self, known_ids: set[str] | None = None) -> list[NormalizedTask]:
        if self._mode == "cdp":
            return self._fetch_tasks_cdp()
        """token 模式（DevTools 直拷）：course/query 取课程 → queryHomeworks 按
        {courseIds, scene:"homework", pageSize, pageNo} 分页拉全部作业。"""
        now = datetime.now()
        self.ensure_access_token()
        try:
            courses = self._query_courses()
        except AdapterError as exc:
            logger.warning("smartestu course query failed: %s", exc)
            courses = []
        course_ids = [int(c["id"]) for c in courses if str(c.get("id", "")).isdigit()]
        tasks: list[NormalizedTask] = []
        seen: set[str] = set()
        # 分页拉取（服务端 pageTotal 为总页数；上限 5 页防失控）
        for page_no in range(1, 6):
            body = {"courseIds": course_ids, "scene": "homework", "pageSize": 50, "pageNo": page_no}
            try:
                data = self._post_json(QUERY_HOMEWORKS_URL, body)
            except CredentialInvalidError:
                raise
            except AdapterError as exc:
                logger.warning("smartestu queryHomeworks failed: %s", exc)
                break
            payload = data.get("data", {}) if isinstance(data, dict) else {}
            page_total = int(payload.get("pageTotal") or 1)
            groups = payload.get("courseHomeworkDTOList", []) or []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                course_hint = str(group.get("courseName", ""))
                for item in group.get("studentCourseHomeworkDTOList", []) or []:
                    task = normalize_homework_item(item, course_hint, course_id=group.get("courseId"))
                    if task is None or task.external_id in seen:
                        continue
                    seen.add(task.external_id)
                    task.fetched_at = now
                    tasks.append(task)
            if page_no >= max(1, min(page_total, 5)):
                break
        return tasks

    # ------------------------------------------------------------ internals
    def _fetch_tasks_cdp(self) -> list[NormalizedTask]:
        """CDP 通道模式：cookie 快照 → 受管 headless Chrome → 页内抓取。

        轮换即落盘：页面握手会轮换 cookie，on_rotate 第一时间写回存储。
        """
        now = datetime.now()
        _courses, groups, new_jar = fetch_via_cdp(self._cookie_jar)
        if new_jar != self._cookie_jar:
            self._capture_jar_rotation(new_jar)
        tasks: list[NormalizedTask] = []
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            course_hint = str(group.get("courseName", ""))
            for item in group.get("studentCourseHomeworkDTOList", []) or []:
                task = normalize_homework_item(item, course_hint, course_id=group.get("courseId"))
                if task is None or task.external_id in seen:
                    continue
                seen.add(task.external_id)
                task.fetched_at = now
                tasks.append(task)
        return tasks

    def _query_courses(self) -> list[dict[str, Any]]:
        data = self._post_json(COURSE_QUERY_URL, {})
        courses: list[dict[str, Any]] = []
        for item in iter_homework_arrays(data):
            cid = _find_first(item, ("id", "courseId"))
            name = _find_first(item, ("name", "courseName", "title"))
            if cid is not None and name:
                courses.append({"id": str(cid), "name": str(name)})
            if len(courses) >= 50:
                break
        return courses

    def _post_json(self, url: str, body: dict[str, Any]) -> Any:
        self.ensure_access_token()
        payload = json.dumps(body or {}, ensure_ascii=False)

        def _do() -> httpx.Response:
            return self._client.post(
                url, content=payload, headers={"Content-Type": "application/json"}
            )

        resp = _do()
        if resp.status_code == 401 and self._mode == "token":
            # access token 过期：refresh 一次并重放
            if self._try_refresh():
                resp = _do()
        if resp.status_code == 401:
            raise CredentialInvalidError("数你最灵 token 已失效且刷新失败，请重新登录获取")
        if resp.status_code != 200:
            raise AdapterError(f"http {resp.status_code} from {url}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(f"non-json response from {url}") from exc

    def _try_refresh(self) -> bool:
        # 无 refreshToken 时不发请求：refresh:<jwt> 模式已废弃，token 模式下
        # _refresh_token 为空，发出去只会是空 cookie（既无意义又是多余的凭据外发）。
        if not self._refresh_token:
            return False
        try:
            resp = self._client.post(
                REFRESH_URL,
                content="",
                headers={"Cookie": f"refreshToken={self._refresh_token}"},
            )
        except AdapterError:
            return False
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        new_token = data.get("token") or data.get("accessToken") or ""
        if not new_token or not isinstance(new_token, str):
            return False
        self._token = new_token
        self._client.set_auth_header(new_token)
        set_cookie = resp.headers.get("set-cookie", "")
        m = re.search(r"refreshToken=([^;\s]+)", set_cookie)
        if m and m.group(1) != self._refresh_token:
            self._capture_rotation(m.group(1))
        logger.info("smartestu access token refreshed via refreshToken")
        return True


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def is_probably_jwt(token: str) -> bool:
    return bool(_TOKEN_RE.match(token.strip()))
