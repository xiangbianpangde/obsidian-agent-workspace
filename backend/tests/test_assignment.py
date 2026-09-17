"""Assignment platform integration tests (guard / storage / parsers / API).

全部离线：适配器网络层通过注入 FakeClient 模拟，绝不请求真实平台。
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.assignment.adapters import chaoxing as cx  # noqa: E402
from backend.app.assignment.adapters import smartestu as se  # noqa: E402
from backend.app.assignment.guard import GuardError, validate_url  # noqa: E402
from backend.app.assignment.http_client import (  # noqa: E402
    CredentialInvalidError,
)
from backend.app.assignment.models import NormalizedTask  # noqa: E402
from backend.app.assignment.storage import AssignmentStorage  # noqa: E402


def _make_task(external_id: str = "work-1", **overrides) -> NormalizedTask:
    defaults = {
        "platform": "chaoxing",
        "external_id": external_id,
        "course_name": "深度学习",
        "title": "第一次作业",
        "due_at": datetime(2026, 9, 20, 23, 59),
        "status": "unsubmitted",
        "score": None,
        "detail_url": "https://mooc1.chaoxing.com/mooc-ans/api/work?a=1",
        "raw": {"k": "v"},
    }
    defaults.update(overrides)
    return NormalizedTask(**defaults)


# --------------------------------------------------------------------------- guard
class TestOutboundGuard(unittest.TestCase):
    def test_allows_whitelisted_https(self):
        url = validate_url("https://i.chaoxing.com/base?t=1")
        self.assertTrue(url.startswith("https://i.chaoxing.com"))

    def test_allows_whitelisted_host_when_dns_is_available(self):
        """The public-IP check needs DNS, so a resolver outage must not be
        reported as a guard failure.

        This test used to live in test_allows_whitelisted_https and therefore
        failed on any machine without DNS, which says nothing about whether the
        guard works. The whitelist decision itself is covered above; here we
        only exercise the IP check when it can actually run.
        """
        import socket

        try:
            socket.gethostbyname("smartestu.cn")
        except OSError:
            self.skipTest("DNS unavailable; cannot exercise the public-IP check")
        self.assertTrue(
            validate_url("https://smartestu.cn/api/homework/student/portal-summary")
        )

    def test_upgrades_http_to_https(self):
        self.assertTrue(validate_url("http://mooc1.chaoxing.com/x").startswith("https://"))

    def test_rejects_private_and_loopback(self):
        for bad in (
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://10.0.0.5/x",
            "http://192.168.1.10/x",
            "http://169.254.169.254/latest",
            "http://172.16.0.1/x",
            "http://[::1]/x",
        ):
            with self.assertRaises(GuardError, msg=bad):
                validate_url(bad)

    def test_rejects_non_whitelisted_hosts(self):
        for bad in (
            "https://evil.example.com/x",
            "https://chaoxing.com.evil.com/x",
            "https://notchaoxing.com/x",
        ):
            with self.assertRaises(GuardError, msg=bad):
                validate_url(bad)

    def test_rejects_bad_scheme_and_ports(self):
        with self.assertRaises(GuardError):
            validate_url("ftp://chaoxing.com/x")
        with self.assertRaises(GuardError):
            validate_url("https://chaoxing.com:8080/x")


# ------------------------------------------------------------------------- storage
class TestAssignmentStorage(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="assignment_test_"))
        self.storage = AssignmentStorage(self.base / "platforms.db")

    def tearDown(self):
        self.storage.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_credential_roundtrip_and_permissions(self):
        self.storage.save_credential("chaoxing", "UID=1; fid=2; secret=yes")
        self.assertEqual(self.storage.get_credential("chaoxing"), "UID=1; fid=2; secret=yes")
        db_bytes = (self.base / "platforms.db").read_bytes()
        self.assertNotIn(b"secret=yes", db_bytes)  # 落库必须是密文
        mode = stat.S_IMODE(os.stat(self.base / "credential.key").st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.base).st_mode), 0o700)

    def test_credential_never_returned_via_status(self):
        self.storage.save_credential("smartestu", "eyJhbGci.x.y")
        for s in self.storage.credential_status():
            self.assertNotIn("eyJhbGci.x.y", str(s))
        self.assertEqual(self.storage.credential_preview("smartestu"), "eyJhbG…(12 chars)")

    def test_disable_is_zero_delete(self):
        self.storage.save_credential("chaoxing", "UID=1")
        self.assertTrue(self.storage.disable_credential("chaoxing"))
        self.assertIsNone(self.storage.get_credential("chaoxing"))
        self.assertFalse(self.storage.credential_status()[0]["configured"])

    def test_task_upsert_and_stale(self):
        self.storage.upsert_tasks([_make_task("w1"), _make_task("w2")])
        stats = self.storage.upsert_tasks([_make_task("w1")])
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["updated"], 1)
        stale = self.storage.mark_stale_except("chaoxing", ["w1"])
        self.assertEqual(stale, 1)  # w2 失效
        rows = self.storage.list_tasks(platform="chaoxing")
        self.assertEqual([r["external_id"] for r in rows], ["w1"])
        rows_all = self.storage.list_tasks(platform="chaoxing", include_stale=True)
        self.assertEqual(len(rows_all), 2)

    def test_task_validation_rejects_bad_rows(self):
        bad = _make_task(status="bogus")
        with self.assertRaises(ValueError):
            self.storage.upsert_tasks([bad])

    def test_sync_journal(self):
        sid = self.storage.start_sync("chaoxing")
        self.storage.finish_sync(sid, True, 3, "ok inserted=3 updated=0")
        last = self.storage.last_sync("chaoxing")
        self.assertTrue(last["ok"])
        self.assertEqual(last["task_count"], 3)


# --------------------------------------------------------------- chaoxing parsing
class TestChaoxingParsers(unittest.TestCase):
    def test_cookie_parse(self):
        c = cx.parse_cookie_string("UID=123_X; fid=2049; vc3=a=b")
        self.assertEqual(c["UID"], "123_X")
        self.assertEqual(c["vc3"], "a=b")

    def test_course_list(self):
        html = (
            '<div class="course" id="c1"><input class="clazzId" value="99"/>'
            '<input class="courseId" value="204"/><a href="?cpi=77&x=1"></a>'
            '<span class="course-name" title="深度学习">深度学习</span>'
            '<p class="color3" title="王老师">王老师</p></div>'
            '<div class="course" id="c2"><span class="course-name" title="坏块"></span></div>'
        )
        courses = cx.parse_course_list_html(html)
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]["courseId"], "204")
        self.assertEqual(courses[0]["clazzId"], "99")
        self.assertEqual(courses[0]["cpi"], "77")
        self.assertEqual(courses[0]["teacher"], "王老师")

    def test_marg_and_work_jobs(self):
        html = (
            'mArg = {"defaults":{"ktoken":"KT"},"attachments":'
            '[{"type":"workid","jobid":"work-8899","enc":"E","mid":"M","aid":"7","property":{"title":"作业一"}},'
            '{"type":"video","jobid":"v1"},{"type":"workid","jobid":"work-1","isPassed":true,"property":{}}]};'
        )
        jobs = cx.extract_work_jobs(cx.extract_marg(html))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["jobid"], "work-8899")
        self.assertEqual(jobs[0]["title"], "作业一")

    def test_chapter_points(self):
        html = (
            '<div class="chapter_unit"><li><div id="cur10001"><a class="clicktitle">第一章</a>'
            '<input class="knowledgeJobCount" value="2"/></div></li></div>'
            '<div class="chapter_unit"><li><div id="cur10002"><a class="clicktitle">第二章</a></div></li></div>'
        )
        pts = cx.parse_chapter_points(html)
        self.assertEqual([p["id"] for p in pts], ["10001", "10002"])
        self.assertEqual(pts[0]["jobCount"], "2")

    def test_work_page_parse(self):
        w = cx.parse_work_page("截止时间: 2026-09-20 23:59 未提交")
        self.assertEqual(w["due_at"], datetime(2026, 9, 20, 23, 59))
        self.assertEqual(w["status"], "unsubmitted")
        w2 = cx.parse_work_page("截止 2026-09-01 得分：85.5 分")
        self.assertEqual(w2["status"], "graded")
        self.assertEqual(w2["score"], 85.5)
        w3 = cx.parse_work_page("<html>empty</html>")
        self.assertEqual(w3["status"], "unknown")

    def test_deadline_normalization(self):
        self.assertEqual(cx.parse_deadline_text("2026-09-15 08:30"), datetime(2026, 9, 15, 8, 30))
        self.assertIsNotNone(cx.parse_deadline_text(1789000000000))
        self.assertIsNone(cx.parse_deadline_text("garbage"))


# ---------------------------------------------------------------------- smartestu
class TestSmartestuNormalization(unittest.TestCase):
    def test_normalize_real_dto(self):
        """实测响应结构：studentCourseHomeworkDTO 条目。"""
        t = se.normalize_homework_item(
            {
                "id": 31585,
                "name": "作业2",
                "courseId": 4377,
                "courseName": "06计科+国安-概率统计",
                "endTime": "2026-09-14T15:00:45.400Z",
                "submission_status": "not_submitted",
                "review_status": "not_reviewed",
                "score": 0,
            }
        )
        self.assertEqual(t.external_id, "hw-4377-31585")
        self.assertEqual(t.title, "作业2")
        self.assertEqual(t.course_name, "06计科+国安-概率统计")
        self.assertEqual(t.status, "unsubmitted")
        # UTC ISO → 本地时间（+8）
        self.assertIsNotNone(t.due_at)
        self.assertEqual(t.due_at.hour, 23)
        # 已批改带分
        t3 = se.normalize_homework_item(
            {"id": 43, "name": "Q3", "courseId": 1, "submission_status": "submitted",
             "review_status": "reviewed", "score": 92.5}
        )
        self.assertEqual(t3.status, "graded")
        self.assertEqual(t3.score, 92.5)
        # 缺 id/name 的跳过
        self.assertIsNone(se.normalize_homework_item({"foo": 1}))

    def test_normalize_legacy_tolerant(self):
        """容错兜底：无 courseName 时用 course_hint；endTime 缺失为 None。"""
        t2 = se.normalize_homework_item(
            {"id": 42, "name": "Q2", "submission_status": "submitted"},
            course_hint="高数",
            course_id=999,
        )
        self.assertEqual(t2.external_id, "hw-999-42")
        self.assertEqual(t2.course_name, "高数")
        self.assertEqual(t2.status, "submitted")
        self.assertIsNone(t2.due_at)

    def test_adapter_rejects_non_jwt(self):
        with self.assertRaises(CredentialInvalidError):
            se.SmartestuAdapter("garbage")


class TestSmartestuRefreshModeRetired(unittest.TestCase):
    """refresh:<jwt> 模式已废弃，构造期即拒绝。

    2026-09 平台升级为 __Host-auth_* cookie 协议，服务端不再接受
    POST /api/auth/refresh 换取 access token。实测证据
    （~/.personal-ai-workspace/assignment/assignment_platforms.db）：
      09-12 09:24 与 09:39 两次同步均以 "refreshToken 已失效" 失败；
      09-12 09:51 改用 cookies: 快照后成功抓取 19 条作业。
    故 refresh: 前缀在构造期即被拒绝，并给出 cookies: 迁移提示。
    """

    def test_refresh_prefix_rejected_with_migration_hint(self):
        client = FakeClient({})
        with self.assertRaises(CredentialInvalidError) as ctx:
            se.SmartestuAdapter("refresh:eyJold.a.b", client=client)
        self.assertIn("cookie", str(ctx.exception))

    def test_refresh_prefix_rejected_before_any_network_call(self):
        """拒绝必须发生在构造期，不得先发出任何网络请求。"""
        client = FakeClient({})
        with self.assertRaises(CredentialInvalidError):
            se.SmartestuAdapter("refresh:eyJr.a.b", client=client)
        self.assertEqual(client.calls, [])

    def test_token_mode_has_no_pending_refresh_state(self):
        """token 模式下不得残留 refresh 专用状态（悬挂引用回归防护）。"""
        adapter = se.SmartestuAdapter("eyJhbGciOiJIUzI1NiJ9.a.b", client=FakeClient({}))
        self.assertFalse(hasattr(adapter, "_refresh_mode"))
        self.assertEqual(adapter._mode, "token")
        self.assertIsNone(adapter.ensure_access_token())


# ------------------------------------------------------------------ fake client
class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self.headers = headers or {}
        self.is_redirect = False

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeClient:
    """Records requests and returns canned responses per URL substring."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list = []
        self.auth_header: str | None = None

    def set_auth_header(self, token: str) -> None:
        self.auth_header = f"Bearer {token}"

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._route(url)

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return self._route(url)

    def _route(self, url):
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        return FakeResponse(status_code=404, text="not found")


class TestChaoxingAdapterOnline(unittest.TestCase):
    def test_missing_uid_rejected(self):
        with self.assertRaises(CredentialInvalidError):
            cx.ChaoxingAdapter("fid=1; vc3=x")

    def test_login_redirect_detected(self):
        client = FakeClient(
            {
                "courselistdata": FakeResponse(
                    status_code=200, text="<a href='https://passport2.chaoxing.com/login'>登录</a>"
                )
            }
        )
        adapter = cx.ChaoxingAdapter("UID=1; fid=2", client=client, rate_delay=(0, 0))
        with self.assertRaises(CredentialInvalidError):
            adapter.validate()

    def test_chaoxing_error_page_detected(self):
        """实测：无效 cookie 时平台经 302 落地通用错误页（HTTP 200）。"""
        client = FakeClient(
            {
                "courselistdata": FakeResponse(
                    status_code=200,
                    text="<html><title>您所浏览的页面暂时不能访问</title></html>",
                )
            }
        )
        adapter = cx.ChaoxingAdapter("UID=1; fid=2", client=client, rate_delay=(0, 0))
        with self.assertRaises(CredentialInvalidError):
            adapter.validate()

    def test_fetch_tasks_end_to_end(self):
        course_html = (
            '<div class="course" id="c1"><input class="clazzId" value="99"/><input class="courseId" value="204"/>'
            '<a href="?cpi=77"></a><span class="course-name" title="深度学习"></span></div>'
        )
        middle_html = '<input name="enc" value="abc123def456"/>'
        worklist_html = (
            '<div class="bottomList"><ul>'
            '<li data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=204&classId=99&cpi=77&workId=55128893&answerId=1&enc=e1" aria-label="第一章作业 ; 未完成">'
            '<p class="overHidden2 fl">第一章作业</p><p class="status fl">未完成</p></li>'
            '</ul></div>'
        )
        task_html = "作业详情 截止时间: 2026-09-20 23:59 未提交"
        client = FakeClient(
            {
                "courselistdata": FakeResponse(status_code=200, text=course_html),
                "stucoursemiddle": FakeResponse(status_code=200, text=middle_html),
                "mooc2/work/list": FakeResponse(status_code=200, text=worklist_html),
                "mooc-ans/mooc2/work/task": FakeResponse(status_code=200, text=task_html),
            }
        )
        adapter = cx.ChaoxingAdapter("UID=1; fid=2", client=client, rate_delay=(0, 0))
        tasks = adapter.fetch_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t.platform, "chaoxing")
        self.assertEqual(t.external_id, "work-55128893")
        self.assertEqual(t.title, "第一章作业")
        self.assertEqual(t.course_name, "深度学习")
        self.assertEqual(t.due_at, datetime(2026, 9, 20, 23, 59))
        self.assertEqual(t.status, "unsubmitted")
        # 请求里必须带会话 enc 与静态作业签名
        work_req = next(url for _, url in client.calls if "work/list" in url)
        self.assertIn("stuenc=abc123def456", work_req)
        self.assertIn("enc=5d5f47f848ed52daeb0f6045c4f2ec15", work_req)
        # 所有出站 URL 都在白名单域内
        for _, url in client.calls:
            self.assertTrue(url.startswith("https://") and "chaoxing.com" in url, url)


class TestSmartestuAdapterOnline(unittest.TestCase):
    def test_401_raises_credential_invalid(self):
        client = FakeClient({"portal-summary": FakeResponse(status_code=401, json_data={})})
        adapter = se.SmartestuAdapter("eyJhbGciOiJIUzI1NiJ9.a.b", client=client)
        with self.assertRaises(CredentialInvalidError):
            adapter.validate()

    def test_token_mode_401_does_not_leak_empty_refresh_cookie(self):
        """token 模式下 _refresh_token 为空，绝不得发出空 refreshToken cookie。

        回归：废弃 refresh: 模式后，若仍无条件调用 _try_refresh()，
        会向 /api/auth/refresh 发送 "Cookie: refreshToken=" 空值请求。
        """
        client = FakeClient({"portal-summary": FakeResponse(status_code=401, json_data={})})
        adapter = se.SmartestuAdapter("eyJhbGciOiJIUzI1NiJ9.a.b", client=client)
        with self.assertRaises(CredentialInvalidError):
            adapter.validate()
        refresh_calls = [url for _, url in client.calls if "auth/refresh" in url]
        self.assertEqual(refresh_calls, [], "不得向 auth/refresh 发出空凭证请求")

    def test_try_refresh_returns_false_without_refresh_token(self):
        client = FakeClient({})
        adapter = se.SmartestuAdapter("eyJhbGciOiJIUzI1NiJ9.a.b", client=client)
        self.assertFalse(adapter._try_refresh())
        self.assertEqual(client.calls, [])

    def test_cdp_mode_never_calls_refresh_endpoint(self):
        """CDP cookie 模式必须完全绕开 refresh 端点（新协议唯一稳定路径）。"""
        client = FakeClient({"portal-summary": FakeResponse(status_code=401, json_data={})})
        adapter = se.SmartestuAdapter("cookies:__Host-auth_x=1; SESSION=y", client=client)
        self.assertEqual(adapter._mode, "cdp")
        self.assertEqual(adapter._refresh_token, "")
        self.assertFalse(adapter._try_refresh())

    def test_fetch_tasks_end_to_end(self):
        """新链路：course/query → queryHomeworks(courseIds+scene) 分页。"""
        courses_resp = FakeResponse(
            status_code=200,
            json_data={"data": [{"id": 4377, "name": "概率统计"}, {"bad": True}]},
        )

        def homeworks_resp(**kw):
            return FakeResponse(
                status_code=200,
                json_data={
                    "data": {
                        "courseHomeworkDTOList": [
                            {
                                "courseId": 4377,
                                "courseName": "概率统计",
                                "studentCourseHomeworkDTOList": [
                                    {
                                        "id": 31585,
                                        "name": "作业2",
                                        "endTime": "2026-09-14T15:00:45.400Z",
                                        "submission_status": "not_submitted",
                                        "review_status": "not_reviewed",
                                    },
                                    {"bad": "row"},
                                ],
                            }
                        ],
                        "pageTotal": 1,
                        "totalCount": 1,
                    }
                },
            )

        client = FakeClient({"course/query": courses_resp, "mark/queryHomeworks": homeworks_resp()})
        adapter = se.SmartestuAdapter("eyJhbGciOiJIUzI1NiJ9.a.b", client=client)
        tasks = adapter.fetch_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t.external_id, "hw-4377-31585")
        self.assertEqual(t.status, "unsubmitted")
        self.assertEqual(t.course_name, "概率统计")
        # 请求体必须带 courseIds + scene
        hw_req = next(url for _, url in client.calls if "queryHomeworks" in url)
        self.assertIn("queryHomeworks", hw_req)
        self.assertEqual(tasks[0].status, "unsubmitted")


# ---------------------------------------------------------------------------- API
class TestAssignmentAPI(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="assignment_api_test_"))
        self.storage = AssignmentStorage(self.base / "platforms.db")
        from backend.app.api import assignment as api_mod

        self._api_mod = api_mod
        self._prev = api_mod._storage
        api_mod._storage = self.storage
        from backend.app.main import app

        self.client = TestClient(app)

    def tearDown(self):
        self._api_mod._storage = self._prev
        self.storage.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_credentials_endpoint_masks_secret(self):
        self.storage.save_credential("chaoxing", "UID=supersecret; fid=2")
        r = self.client.get("/api/assignment/credentials")
        self.assertEqual(r.status_code, 200)
        self.assertIn("supersecret", self.storage.get_credential("chaoxing"))
        self.assertNotIn("supersecret", r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store, no-cache, must-revalidate")

    def test_tasks_endpoints(self):
        soon = datetime.fromtimestamp(datetime.now().timestamp() + 3600 * 12)
        self.storage.upsert_tasks(
            [_make_task("w1", due_at=soon), _make_task("w2", status="submitted", due_at=None)]
        )
        r = self.client.get("/api/assignment/tasks")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        r2 = self.client.get("/api/assignment/tasks?status=unsubmitted")
        self.assertEqual(r2.json()["count"], 1)
        r3 = self.client.get("/api/assignment/tasks/due-soon?within_hours=48")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["count"], 1)

    def test_unknown_platform_404(self):
        r = self.client.post("/api/assignment/credentials/nope", json={"credential": "x" * 20})
        self.assertEqual(r.status_code, 404)

    def test_bad_credential_400(self):
        r = self.client.post(
            "/api/assignment/credentials/chaoxing", json={"credential": "missing=uid"}
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------- sync rate limiting
class TestSyncThrottle(unittest.TestCase):
    """docs/05 §4.3: sync is capped at 1 per 5 minutes to respect platform
    rate limits. Manual sync is the only trigger; there is no auto-polling."""

    def _storage(self):
        import tempfile
        from pathlib import Path
        from backend.app.assignment.storage import AssignmentStorage

        return AssignmentStorage(Path(tempfile.mkdtemp()) / "a.db")

    def test_first_sync_is_not_throttled(self):
        from backend.app.assignment import sync as sm

        storage = self._storage()
        self.assertEqual(sm._throttle_remaining(storage, "chaoxing"), 0)

    def test_immediate_resync_is_throttled(self):
        from backend.app.assignment import sync as sm

        storage = self._storage()
        sync_id = storage.start_sync("chaoxing")
        storage.finish_sync(sync_id, ok=True, task_count=1, message="ok")
        remaining = sm._throttle_remaining(storage, "chaoxing")
        self.assertGreater(remaining, 0)

    def test_throttled_sync_reports_retry_after(self):
        from backend.app.assignment import sync as sm

        storage = self._storage()
        sync_id = storage.start_sync("chaoxing")
        storage.finish_sync(sync_id, ok=True, task_count=1, message="ok")
        result = sm.sync_platform(storage, "chaoxing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "throttled")
        self.assertGreater(result["retry_after"], 0)

    def test_throttle_expires_after_window(self):
        import sqlite3
        import time

        from backend.app.assignment import sync as sm

        storage = self._storage()
        sync_id = storage.start_sync("chaoxing")
        storage.finish_sync(sync_id, ok=True, task_count=1, message="ok")
        conn = sqlite3.connect(storage.db_path)
        conn.execute(
            "UPDATE sync_journal SET started_at = ?",
            (int((time.time() - sm.SYNC_MIN_INTERVAL_SECONDS - 100) * 1000),),
        )
        conn.commit()
        conn.close()
        self.assertEqual(sm._throttle_remaining(storage, "chaoxing"), 0)

    def test_throttle_is_per_platform(self):
        """A chaoxing sync must not block smartestu."""
        from backend.app.assignment import sync as sm

        storage = self._storage()
        sync_id = storage.start_sync("chaoxing")
        storage.finish_sync(sync_id, ok=True, task_count=1, message="ok")
        self.assertGreater(sm._throttle_remaining(storage, "chaoxing"), 0)
        self.assertEqual(sm._throttle_remaining(storage, "smartestu"), 0)

    def test_unknown_platform_is_rejected_without_touching_network(self):
        from backend.app.assignment import sync as sm

        storage = self._storage()
        result = sm.sync_platform(storage, "not_a_platform")
        self.assertEqual(result["error"], "unknown_platform")


class TestSyncThrottleHTTP(unittest.TestCase):
    def test_sync_endpoint_returns_429_when_throttled(self):
        import tempfile
        from pathlib import Path

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import backend.app.api.assignment as api
        from backend.app.assignment.storage import AssignmentStorage

        storage = AssignmentStorage(Path(tempfile.mkdtemp()) / "a.db")
        sync_id = storage.start_sync("chaoxing")
        storage.finish_sync(sync_id, ok=True, task_count=1, message="ok")

        app = FastAPI()
        app.include_router(api.router)
        original = api.get_storage
        api.get_storage = lambda: storage
        try:
            client = TestClient(app)
            response = client.post("/api/assignment/sync/chaoxing")
            self.assertEqual(response.status_code, 429)
            self.assertIn("retry-after", {k.lower() for k in response.headers})
            self.assertEqual(response.json()["error"], "throttled")
            self.assertIn("no-store", response.headers.get("cache-control", ""))
        finally:
            api.get_storage = original
