"""CDP 通道：用受管 headless Chrome 承载数你最灵的协议握手与数据抓取。

背景：数你最灵 2026-09 升级认证协议（__Host-auth_* cookie + 服务端轮换），
纯 HTTP 复现登录协议被服务端拒绝（426 AUTH_CLIENT_UPDATE_REQUIRED）。
唯一稳定路径是在真实浏览器上下文里让页面自己完成握手，再由页内 fetch 取数。

流程（fetch_via_cdp）：
  1. spawn 隔离 headless Chrome（工作台数据目录下的专用 profile，空库即可）
  2. CDP Storage.setCookies 注入凭证快照（cookie jar 文本）
  3. 打开 smartestu.cn/assignment，等页面完成协议握手（轮换发生在副本内）
  4. 页内 fetch：course/query → queryHomeworks 分页 → portal-summary
  5. Network.getCookies 回读轮换后的新快照
  6. finally 杀掉 Chrome，返回 (courses, homework_groups, new_jar)

凭证快照格式（credential 字符串）："cookies:<name>=<value>; name2=value2; ..."

安全边界：subprocess 参数为固定列表（shell=False 默认）；可执行文件仅来自
内置候选常量表；cookie 值只进 CDP websocket JSON，绝不进入命令行。
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..http_client import AdapterError

logger = logging.getLogger(__name__)

BASE_URL = "https://smartestu.cn"
CDP_PORT = 9224
HANDSHAKE_WAIT_S = 8.0
MAX_PAGES = 5

_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
)

PROFILE_DIR = Path.home() / ".personal-ai-workspace" / "assignment" / "cdp_profile"
USER_DATA_ARG = "--user-data-dir=" + str(PROFILE_DIR)
DEBUG_PORT_ARG = "--remote-debugging-port=" + str(CDP_PORT)


def parse_cookie_jar(jar_text: str) -> list[dict[str, Any]]:
    """Parses 'name=value; name2=value2' into CDP Storage.setCookies objects."""
    out: list[dict[str, Any]] = []
    for part in jar_text.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if not name or not value:
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }
        if name.startswith("__Host-"):
            cookie["domain"] = "smartestu.cn"  # __Host-: 精确域
        else:
            cookie["domain"] = ".smartestu.cn"
        out.append(cookie)
    return out


def jar_to_text(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in sorted(cookies, key=lambda c: c["name"]))


def _find_chrome() -> str:
    for candidate in _CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise AdapterError("未找到 Chrome 可执行文件，无法建立 CDP 通道")


class _CdpSession:
    """Minimal CDP websocket session (version/create-target/evaluate/cookies)."""

    def __init__(self, port: int):
        import urllib.request

        import websocket  # websocket-client

        deadline = time.time() + 10
        ver: dict[str, Any] | None = None
        last_exc: Exception | None = None
        while time.time() < deadline:
            try:
                ver = json.load(
                    urllib.request.urlopen("http://127.0.0.1:" + str(port) + "/json/version", timeout=3)
                )
                break
            except Exception as exc:  # noqa: BLE001 - 端口未就绪时重试
                last_exc = exc
                time.sleep(0.5)
        if ver is None:
            raise AdapterError("CDP endpoint not ready: " + repr(last_exc))
        self._ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=60)
        self._mid = 0

    def cmd(
        self, method: str, params: dict[str, Any] | None = None, session: str | None = None
    ) -> dict[str, Any]:
        self._mid += 1
        msg: dict[str, Any] = {"id": self._mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._ws.send(json.dumps(msg))
        while True:
            m = json.loads(self._ws.recv())
            if m.get("id") == self._mid:
                if "error" in m:
                    raise AdapterError("CDP " + method + " error: " + json.dumps(m["error"]))
                return m

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


_PAGE_FETCH_SCRIPT = """
(async () => {
  const H = {'Content-Type': 'application/json'};
  const rc = await fetch('/api/homework/student/course/query', {method:'POST', headers:H, body:'{}'});
  if (rc.status !== 200) return JSON.stringify({error: 'course/query http ' + rc.status});
  const courses = (await rc.json()).data || [];
  const ids = courses.map(c => c.id);
  const groups = [];
  let page_total = 1;
  for (let page = 1; page <= Math.min(page_total, 5); page++) {
    const rh = await fetch('/api/homework/student/mark/queryHomeworks', {method:'POST', headers:H,
      body: JSON.stringify({courseIds: ids, scene: 'homework', pageSize: 50, pageNo: page})});
    if (rh.status !== 200) return JSON.stringify({error: 'queryHomeworks http ' + rh.status});
    const hj = await rh.json();
    page_total = hj.data ? (hj.data.pageTotal || 1) : 1;
    for (const g of (hj.data ? hj.data.courseHomeworkDTOList : []) || []) groups.push(g);
    if (page >= page_total) break;
  }
  const rs = await fetch('/api/homework/student/portal-summary', {method:'POST', headers:H, body:'{}'});
  const pending = rs.status === 200 ? ((await rs.json()).data || {}).pendingHomeworkCount : null;
  return JSON.stringify({courses, groups, pending});
})()
"""


def fetch_via_cdp(cookie_jar_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Runs the full CDP channel; returns (courses, homework_groups, new_jar_text)."""
    if not cookie_jar_text.strip():
        raise AdapterError("cookie 快照为空")
    chrome = _find_chrome()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    chrome_args = [
        chrome,
        USER_DATA_ARG,
        DEBUG_PORT_ARG,
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--headless=new",
        "--disable-gpu",
        "about:blank",
    ]
    proc = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session: _CdpSession | None = None
    try:
        session = _CdpSession(CDP_PORT)

        session.cmd("Storage.setCookies", {"cookies": parse_cookie_jar(cookie_jar_text)})
        target = session.cmd("Target.createTarget", {"url": BASE_URL + "/assignment"})
        target_id = target["result"]["targetId"]
        attached = session.cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = attached["result"]["sessionId"]
        time.sleep(HANDSHAKE_WAIT_S)  # 页面完成协议握手（轮换发生在副本内）

        r = session.cmd(
            "Runtime.evaluate",
            {"expression": _PAGE_FETCH_SCRIPT, "awaitPromise": True, "returnByValue": True},
            session=sid,
        )
        value = r.get("result", {}).get("result", {}).get("value")
        if not value:
            raise AdapterError("page evaluate failed: " + json.dumps(r.get("result"))[:300])
        payload = json.loads(value)
        if "error" in payload:
            raise AdapterError("page API error: " + str(payload["error"]))

        rc = session.cmd("Network.getCookies", {"urls": [BASE_URL]}, session=sid)
        new_jar = jar_to_text(rc["result"]["cookies"])
        session.cmd("Target.closeTarget", {"targetId": target_id})
        return payload["courses"], payload["groups"], new_jar
    finally:
        if session is not None:
            session.close()
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
