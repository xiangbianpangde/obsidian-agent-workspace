"""学习通 (Chaoxing) read-only assignment adapter.

认证：用户从浏览器导入 cookie（关键项 _uid/UID、fid），本模块绝不实现账号密码登录。
只读链路（对齐开源项目 Samueli924/chaoxing 的已验证路径）：
  1. POST mooc2-ans/visit/courselistdata → 课程列表 HTML
  2. GET  mooc2-ans/mycourse/studentcourse → 章节树（cur<id> 点位）
  3. GET  mooc1/mooc-ans/knowledge/cards → mArg JSON → type=="workid" 的作业附件
  4. GET  mooc1/mooc-ans/api/work → 作业页 HTML（仅解析标题/截止时间/提交状态，绝不提交表单）
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from ..http_client import AdapterError, BasePlatformClient, CredentialInvalidError
from ..models import NormalizedTask, PlatformName

logger = logging.getLogger(__name__)

COURSE_LIST_URL = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
COURSE_POINT_URL = "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
CARDS_URL = "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards"
WORK_DETAIL_URL = "https://mooc1.chaoxing.com/mooc-ans/api/work"
STU_COURSE_MIDDLE_URL = "https://mooc1.chaoxing.com/visit/stucoursemiddle"
WORK_LIST_URL = "https://mooc1.chaoxing.com/mooc2/work/list"

# 作业模块的静态签名（实测两次不同会话/不同 stuenc 下 iframe 的 enc 均为同一固定值）。
# 平台若轮换此值，作业列表会 fail-closed 退化为"提示页"，此时需人工更新。
WORK_LIST_STATIC_ENC = "5d5f47f848ed52daeb0f6045c4f2ec15"

# 凭证无效时的三类真实表现（实测）：
# 1. 302 -> passport2 登录页；2. 跳转后落地"用户登录/请登录"页；
# 3. 落地"您所浏览的页面暂时不能访问"通用错误页（HTTP 200 也可能返回）。
_LOGIN_MARKERS = (
    "passport2.chaoxing.com",
    "/login",
    "请登录",
    "用户登录",
    "您所浏览的页面暂时不能访问",
)

_DUE_RE = re.compile(
    r"(?:提交)?截止[^<\d]{0,12}?(\d{4})-(\d{1,2})-(\d{1,2})(?:[^\d]{0,3}(\d{1,2}):(\d{2}))?"
)
_SCORE_RE = re.compile(r"得分[^<\d]{0,10}?([\d.]+)")
_SUBMITTED_RE = re.compile(r"已提交|已经提交|查看作业")
_UNSUBMITTED_RE = re.compile(r"未提交|尚未提交|去完成|开始作业")


# --------------------------------------------------------------------------- parsers
def parse_cookie_string(raw: str) -> dict[str, str]:
    """Parses a browser-copied cookie header into a dict."""
    cookies: dict[str, str] = {}
    for part in raw.replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if name:
            cookies[name] = value
    return cookies


class _CourseListParser(HTMLParser):
    """Extracts div.course blocks with clazzId/courseId/cpi/name/teacher.

    courselistdata 返回的 AJAX 片段 div 常不严格配对（浏览器靠容错渲染），
    因此不依赖嵌套计数：以「下一门课 div.course 开始」为块的结束边界。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.courses: list[dict[str, str]] = []
        self._cur: dict[str, str] | None = None

    def _close_current(self) -> None:
        if self._cur is not None:
            self.courses.append(self._cur)
            self._cur = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        ad = {k: (v or "") for k, v in attrs}
        cls = ad.get("class", "")
        if tag == "div" and "course" in cls.split():
            self._close_current()
            self._cur = {"dom_id": ad.get("id", "")}
            return
        if self._cur is None:
            return
        if tag == "input":
            if "clazzId" in cls:
                self._cur["clazzId"] = ad.get("value", "")
            elif "courseId" in cls:
                self._cur["courseId"] = ad.get("value", "")
        elif tag == "a":
            href = ad.get("href", "")
            m = re.search(r"[?&]cpi=([^&]+)", href)
            if m:
                self._cur["cpi"] = m.group(1)
        elif tag == "span" and "course-name" in cls:
            self._cur["title"] = ad.get("title", "") or self._cur.get("title", "")
            if not self._cur["title"]:
                self._capture_text = True
        elif tag == "p" and "color3" in cls and ad.get("title"):
            if not self._cur.get("teacher"):
                self._cur["teacher"] = ad["title"]
        elif tag == "h3" and "inlineBlock" in cls:
            self._capture_text = True

    def handle_data(self, data: str):
        if getattr(self, "_capture_text", False) and self._cur is not None:
            chunk = data.strip()
            if chunk and not self._cur.get("title"):
                self._cur["title"] = chunk

    def handle_endtag(self, tag: str):
        if tag in ("span", "h3", "a"):
            self._capture_text = False

    def close(self) -> None:
        self._close_current()
        super().close()


def parse_course_list_html(html: str) -> list[dict[str, str]]:
    parser = _CourseListParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.warning("course list html parse failed", exc_info=True)
    out = []
    for c in parser.courses:
        if not c.get("courseId") or not c.get("clazzId"):
            continue  # 结构异常的课程块直接跳过，保证 fail-closed
        c.setdefault("cpi", "")
        c.setdefault("title", "")
        c.setdefault("teacher", "")
        out.append(c)
    return out


def extract_marg(html: str) -> dict[str, Any] | None:
    """Extracts the mArg JSON embedded in knowledge card pages."""
    m = re.search(r"mArg\s*=\s*(\{.*?\})\s*;", html, re.S)
    if not m:
        # 兼容空格被压缩的页面变体（与开源实现一致）
        stripped = re.search(r"mArg=\{(.*?)\};", html.replace(" ", ""), re.S)
        if not stripped:
            return None
        m = stripped
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        logger.warning("mArg json decode failed")
        return None


def extract_work_jobs(marg: dict[str, Any]) -> list[dict[str, Any]]:
    """Filters type=='workid' attachments; returns raw job dicts."""
    jobs: list[dict[str, Any]] = []
    for card in marg.get("attachments", []) or []:
        if not isinstance(card, dict) or card.get("type") != "workid":
            continue
        if card.get("isPassed", False):
            continue
        prop = card.get("property", {}) if isinstance(card.get("property"), dict) else {}
        jobs.append(
            {
                "jobid": card.get("jobid", ""),
                "enc": card.get("enc", ""),
                "mid": card.get("mid", ""),
                "aid": card.get("aid", ""),
                "title": prop.get("title", "") or prop.get("name", ""),
            }
        )
    return [j for j in jobs if j["jobid"]]


class _ChapterParser(HTMLParser):
    """Extracts chapter knowledge points: div id="cur<id>" + clicktitle text.

    按 id 累积到 dict，避免 input.knowledgeJobCount 出现在 </a> 之后丢失归属。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._by_id: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._point_id: str | None = None
        self._capture_text = False
        self._text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        ad = {k: (v or "") for k, v in attrs}
        if tag == "div" and ad.get("id", "").startswith("cur"):
            m = re.match(r"^cur(\d{1,20})$", ad.get("id", ""))
            if m:
                self._point_id = m.group(1)
                if self._point_id not in self._by_id:
                    self._by_id[self._point_id] = {
                        "id": self._point_id,
                        "title": "",
                        "jobCount": "1",
                    }
                    self._order.append(self._point_id)
                self._text_chunks = []
        elif self._point_id is None:
            return
        elif tag == "a" and "clicktitle" in ad.get("class", ""):
            self._capture_text = True
        elif tag == "input" and "knowledgeJobCount" in ad.get("class", ""):
            self._by_id[self._point_id]["jobCount"] = ad.get("value", "1") or "1"

    def handle_data(self, data: str):
        if self._capture_text:
            self._text_chunks.append(data)

    def handle_endtag(self, tag: str):
        if tag == "a" and self._capture_text and self._point_id is not None:
            self._capture_text = False
            self._by_id[self._point_id]["title"] = re.sub(r"\s+", "", "".join(self._text_chunks))
            self._text_chunks = []


def parse_chapter_points(html: str) -> list[dict[str, Any]]:
    parser = _ChapterParser()
    try:
        parser.feed(html)
    except Exception:
        logger.warning("chapter html parse failed", exc_info=True)
    return [parser._by_id[pid] for pid in parser._order]


class _WorkListParser(HTMLParser):
    """Parses mooc2/work/list items: <li data="<task url>" aria-label="title ; status">.

    标题/状态同时取自 p.overHidden2 与 p.status（aria-label 为冗余来源）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._cur: dict[str, str] | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        ad = {k: (v or "") for k, v in attrs}
        cls = ad.get("class", "")
        if tag == "li" and ad.get("data", "").startswith("http"):
            self._close()
            self._cur = {"task_url": ad["data"], "title": "", "status": ""}
            label = ad.get("aria-label", "")
            if ";" in label:
                t, _, s = label.partition(";")
                self._cur["title"] = t.strip()
                self._cur["status"] = s.strip()
            return
        if self._cur is None:
            return
        if tag == "p" and "overHidden2" in cls:
            self._capture = "title"
        elif tag == "p" and "status" in cls.split():
            self._capture = "status"

    def handle_data(self, data: str):
        if self._capture and self._cur is not None:
            chunk = data.strip()
            if chunk and not self._cur.get(self._capture or ""):
                self._cur[self._capture or ""] = chunk

    def handle_endtag(self, tag: str):
        if tag == "p":
            self._capture = None

    def close(self) -> None:
        self._close()
        super().close()

    def _close(self) -> None:
        if self._cur is not None and self._cur.get("title"):
            self.items.append(self._cur)
        self._cur = None


def parse_work_list_html(html: str) -> list[dict[str, str]]:
    """Returns [{title, status, task_url, work_id}]; ignores malformed rows."""
    parser = _WorkListParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.warning("work list html parse failed", exc_info=True)
    out = []
    for it in parser.items:
        m = re.search(r"[?&]workId=(\d+)", it.get("task_url", ""))
        if not m:
            continue
        it["work_id"] = m.group(1)
        out.append(it)
    return out


def parse_work_page(html: str) -> dict[str, Any]:
    """Tolerant extraction of due date / submit status / score from work HTML."""
    out: dict[str, Any] = {"due_at": None, "status": "unknown", "score": None}
    m = _DUE_RE.search(html)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 23
        mm = int(m.group(5)) if m.group(5) else 59
        with contextlib.suppress(ValueError):
            out["due_at"] = datetime(y, mo, d, hh, mm)
    sm = _SCORE_RE.search(html)
    if sm:
        with contextlib.suppress(ValueError):
            out["score"] = float(sm.group(1))
    if sm is not None:
        out["status"] = "graded"
    elif _SUBMITTED_RE.search(html):
        out["status"] = "submitted"
    elif _UNSUBMITTED_RE.search(html):
        out["status"] = "unsubmitted"
    return out


def parse_deadline_text(raw: Any) -> datetime | None:
    """Best-effort deadline normalization shared by platform payloads.

    ISO8601（含 Z / 时区偏移）按 UTC 解析后转为本地时间；naive 字符串按本地处理。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ms = float(raw)
        if ms > 1e14:  # microseconds
            ms /= 1000.0
        if ms > 1e11:  # milliseconds
            ms /= 1000.0
        try:
            return datetime.fromtimestamp(ms)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw, str):
        s = raw.strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone()
            return dt.replace(tzinfo=None)
        except ValueError:
            pass
        m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})", s)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
            except ValueError:
                return None
        if s.isdigit():
            return parse_deadline_text(int(s))
    return None


# --------------------------------------------------------------------------- adapter
class ChaoxingAdapter:
    platform: PlatformName = "chaoxing"

    def __init__(
        self,
        cookie_string: str,
        client: BasePlatformClient | None = None,
        rate_delay: tuple[float, float] = (0.6, 1.4),
        max_courses: int = 8,
        max_chapters: int = 15,
        max_work_details: int = 20,
    ):
        cookies = parse_cookie_string(cookie_string)
        uid = cookies.get("_uid") or cookies.get("UID")
        if not uid:
            raise CredentialInvalidError("cookie 中缺少 _uid/UID，请从已登录浏览器完整复制")
        self._cookie_string = cookie_string
        self._client = client or BasePlatformClient(
            headers={
                "Cookie": cookie_string,
                "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction",
            }
        )
        self._rate_delay = rate_delay
        self._max_courses = max_courses
        self._max_chapters = max_chapters
        self._max_work_details = max_work_details

    def _pace(self) -> None:
        time.sleep(random.uniform(*self._rate_delay))

    def _assert_logged_in(self, html: str) -> None:
        if any(marker in html for marker in _LOGIN_MARKERS):
            raise CredentialInvalidError("学习通 cookie 已失效，请重新从浏览器导入")

    # ------------------------------------------------------------ public API
    def validate(self) -> bool:
        self.fetch_course_list()
        return True

    def fetch_course_list(self) -> list[dict[str, str]]:
        self._pace()
        resp = self._client.post(
            COURSE_LIST_URL,
            data={"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if resp.status_code != 200:
            raise AdapterError(f"course list http {resp.status_code}")
        self._assert_logged_in(resp.text)
        return parse_course_list_html(resp.text)

    def fetch_tasks(self, known_ids: set[str] | None = None) -> list[NormalizedTask]:
        """作业列表主链路：每门课 2 个请求（stucoursemiddle → work/list）。

        章节卡片链路（_fetch_course_work_jobs）作为随堂测验的补充源在
        fetch_tasks_chapter() 中保留，默认不在同步里走（请求量大）。
        """
        known = known_ids or set()
        courses = self.fetch_course_list()[: self._max_courses]
        tasks: list[NormalizedTask] = []
        now = datetime.now()
        details_budget = self._max_work_details
        for course in courses:
            self._pace()
            try:
                items = self._fetch_course_work_list(course)
            except AdapterError as exc:
                logger.warning("work list failed for course %s: %s", course.get("title"), exc)
                continue
            for it in items:
                external_id = f"work-{it['work_id']}"
                status = "submitted" if it["status"] == "已完成" else "unsubmitted"
                task = NormalizedTask(
                    platform=self.platform,
                    external_id=external_id,
                    course_name=course.get("title", ""),
                    title=it.get("title") or f"作业 {it['work_id']}",
                    status=status if status == "unsubmitted" else "submitted",
                    detail_url=it.get("task_url", ""),
                    fetched_at=now,
                    raw={"course": course.get("title", ""), "status_text": it["status"]},
                )
                # 截止时间只在未完成的新任务上花预算（详情页），已完成任务不追
                if status == "unsubmitted" and external_id not in known and details_budget > 0:
                    details_budget -= 1
                    self._enrich_from_task_page(task)
                tasks.append(task)
        return tasks

    def fetch_tasks_chapter(self, known_ids: set[str] | None = None) -> list[NormalizedTask]:
        """章节检测链路（随堂测验），可选补充源。"""
        known = known_ids or set()
        courses = self.fetch_course_list()[: self._max_courses]
        tasks: list[NormalizedTask] = []
        now = datetime.now()
        for course in courses:
            jobs = self._fetch_course_work_jobs(course)
            details_budget = self._max_work_details
            for job in jobs:
                external_id = job["jobid"]
                task = NormalizedTask(
                    platform=self.platform,
                    external_id=external_id,
                    course_name=course.get("title", ""),
                    title=job.get("title") or f"作业 {external_id}",
                    status="unknown",
                    fetched_at=now,
                    raw={"course": course.get("title", ""), "jobid": external_id},
                )
                if external_id not in known and details_budget > 0:
                    details_budget -= 1
                    self._enrich_from_work_page(course, job, task)
                tasks.append(task)
        return tasks

    # ------------------------------------------------------------ internals
    def _fetch_course_work_list(self, course: dict[str, str]) -> list[dict[str, str]]:
        """stucoursemiddle → enc → mooc2/work/list（每门课 2 个请求）。"""
        self._pace()
        r = self._client.get(
            STU_COURSE_MIDDLE_URL
            + f"?courseid={course['courseId']}&clazzid={course['clazzId']}&cpi={course.get('cpi', '')}&ismooc2=1&v=2"
        )
        if r.status_code != 200:
            raise AdapterError(f"stucoursemiddle http {r.status_code}")
        self._assert_logged_in(r.text)
        enc_m = re.search(r'name="enc" value="([a-f0-9]+)"', r.text)
        if not enc_m:
            raise AdapterError("stucoursemiddle response has no enc input")
        stuenc = enc_m.group(1)
        self._pace()
        wl = self._client.get(
            WORK_LIST_URL
            + f"?courseId={course['courseId']}&classId={course['clazzId']}&cpi={course.get('cpi', '')}&ut=s"
            + f"&t={int(time.time() * 1000)}&stuenc={stuenc}&enc={WORK_LIST_STATIC_ENC}"
        )
        if wl.status_code != 200:
            raise AdapterError(f"work list http {wl.status_code}")
        self._assert_logged_in(wl.text)
        if "<title>提示</title>" in wl.text:
            # 平台返回的通用无权限/参数失效提示页（签名轮换或参数变更时出现）
            raise AdapterError("work list returned hint page (signature/params stale)")
        return parse_work_list_html(wl.text)

    def _enrich_from_task_page(self, task: NormalizedTask) -> None:
        """未完成任务访问详情页解析截止时间；只读，绝不提交。"""
        url = task.detail_url
        if not url:
            return
        self._pace()
        try:
            resp = self._client.get(url)
        except AdapterError as exc:
            logger.warning("task page fetch failed: %s", exc)
            return
        if resp.status_code != 200:
            return
        self._assert_logged_in(resp.text)
        parsed = parse_work_page(resp.text)
        task.due_at = parsed["due_at"]
        if parsed["score"] is not None:
            task.score = parsed["score"]
        if parsed["status"] != "unknown":
            task.status = parsed["status"]
        task.raw["parsed"] = {
            k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in parsed.items()
        }

    def _fetch_course_work_jobs(self, course: dict[str, str]) -> list[dict[str, Any]]:
        """Walks chapters and collects workid jobs; bounded & rate-limited."""
        self._pace()
        resp = self._client.get(
            COURSE_POINT_URL
            + f"?courseid={course['courseId']}&clazzid={course['clazzId']}&cpi={course.get('cpi', '')}&ut=s",
        )
        if resp.status_code != 200:
            raise AdapterError(f"course page http {resp.status_code}")
        self._assert_logged_in(resp.text)
        points = parse_chapter_points(resp.text)[: self._max_chapters]
        jobs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for point in points:
            for num in (0, 1):
                if int(point.get("jobCount", 1) or 1) <= num:
                    break
                self._pace()
                cards = self._client.get(
                    CARDS_URL
                    + f"?clazzid={course['clazzId']}&courseid={course['courseId']}&knowledgeid={point['id']}&ut=s&cpi={course.get('cpi', '')}&mooc2=1&num={num}",
                )
                if cards.status_code != 200:
                    continue
                marg = extract_marg(cards.text)
                if not marg:
                    continue
                defaults = (
                    marg.get("defaults", {}) if isinstance(marg.get("defaults"), dict) else {}
                )
                for job in extract_work_jobs(marg):
                    job = dict(job)
                    job["knowledgeid"] = point["id"]
                    job["ktoken"] = defaults.get("ktoken", "")
                    job["cpi"] = course.get("cpi", "")
                    job["courseId"] = course["courseId"]
                    job["clazzId"] = course["clazzId"]
                    if job["jobid"] not in seen:
                        seen.add(job["jobid"])
                        jobs.append(job)
        return jobs

    def _enrich_from_work_page(
        self, course: dict[str, str], job: dict[str, Any], task: NormalizedTask
    ) -> None:
        """Fetches the work page once to fill due date / status; never submits."""
        work_id = job["jobid"].replace("work-", "")
        qs = {
            "api": "1",
            "workId": work_id,
            "jobid": job["jobid"],
            "originJobId": job["jobid"],
            "needRedirect": "true",
            "skipHeader": "true",
            "knowledgeid": job["knowledgeid"],
            "ktoken": job["ktoken"],
            "cpi": job["cpi"],
            "ut": "s",
            "clazzId": job["clazzId"],
            "type": "",
            "enc": job["enc"],
            "mooc2": "1",
            "courseid": job["courseId"],
        }
        query = "&".join(f"{k}={v}" for k, v in qs.items())
        detail_url = WORK_DETAIL_URL + "?" + query
        self._pace()
        try:
            resp = self._client.get(detail_url)
        except AdapterError as exc:
            logger.warning("work page fetch failed for %s: %s", job["jobid"], exc)
            task.detail_url = detail_url
            return
        if resp.status_code != 200:
            task.detail_url = detail_url
            return
        self._assert_logged_in(resp.text)
        parsed = parse_work_page(resp.text)
        task.due_at = parsed["due_at"]
        task.status = parsed["status"]
        task.score = parsed["score"]
        task.detail_url = detail_url
        task.raw["parsed"] = {
            k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in parsed.items()
        }
