"""Sync orchestration: storage <-> adapter glue for assignment platforms."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .adapters.chaoxing import ChaoxingAdapter
from .adapters.smartestu import SmartestuAdapter
from .http_client import AdapterError, CredentialInvalidError
from .models import NormalizedTask
from .storage import AssignmentStorage

logger = logging.getLogger(__name__)

# 平台间互斥；同一时刻只允许一个同步任务跑，防风控
sync_lock = threading.Lock()

PLATFORMS = ("chaoxing", "smartestu")

# 风控（docs/05 §4.3）：默认上限 1 次/5 分钟。学习通对高频请求有限制，
# 手动同步是同步触发的唯一入口，前端不提供自动轮询。可用环境变量关闭
# （设为 0）以便本地调试。
SYNC_MIN_INTERVAL_SECONDS = int(os.environ.get("ASSIGNMENT_SYNC_MIN_INTERVAL", "300"))


class SyncThrottled(Exception):
    """同步被频率限制拦截。携带距可重试的剩余秒数。"""

    def __init__(self, platform: str, retry_after: int):
        self.platform = platform
        self.retry_after = retry_after
        super().__init__(f"sync throttled for {platform}; retry in {retry_after}s")


def _throttle_remaining(storage: AssignmentStorage, platform: str) -> int:
    """返回还需等待的秒数；0 表示可以同步。"""
    if SYNC_MIN_INTERVAL_SECONDS <= 0:
        return 0
    last = storage.last_sync(platform)
    if not last:
        return 0
    started_ms = last.get("started_at")
    if not started_ms:
        return 0
    elapsed = time.time() - (int(started_ms) / 1000.0)
    remaining = SYNC_MIN_INTERVAL_SECONDS - int(elapsed)
    return max(0, remaining)


def build_adapter(platform: str, credential: str):
    if platform == "chaoxing":
        return ChaoxingAdapter(credential)
    if platform == "smartestu":
        return SmartestuAdapter(credential)
    raise AdapterError(f"unknown platform: {platform}")


def validate_and_store_credential(
    storage: AssignmentStorage, platform: str, credential: str
) -> dict[str, Any]:
    """Validates the credential with one light platform call, then stores it."""
    if platform not in PLATFORMS:
        raise AdapterError(f"unknown platform: {platform}")
    adapter = build_adapter(platform, credential)  # 构造即校验格式（缺 _uid / 非 JWT 直接抛）

    def on_rotate(new_credential: str) -> None:
        # validate() 里的 refresh 即触发轮换 → 立即落盘，防止后续异常丢新值
        storage.save_credential(platform, new_credential)

    adapter.on_rotate = on_rotate
    if not adapter.validate():
        raise CredentialInvalidError("平台校验未通过（接口返回异常），请检查凭证")
    # 双保险：validate 结束后仍存在轮换值则再存一次（幂等；refresh/jar 两种前缀都由回调写入）
    rotated = getattr(adapter, "rotated_refresh_token", None)
    if rotated:
        credential = "refresh:" + rotated
    rotated_jar = getattr(adapter, "rotated_cookie_jar", None)
    if rotated_jar:
        credential = "cookies:" + rotated_jar
    storage.save_credential(platform, credential)
    storage.mark_validation(platform, True)
    return {"platform": platform, "valid": True, "preview": storage.credential_preview(platform)}


def sync_platform(storage: AssignmentStorage, platform: str) -> dict[str, Any]:
    """Pulls tasks for one platform, upserts, marks stale; journalized."""
    if platform not in PLATFORMS:
        return {
            "platform": platform,
            "ok": False,
            "error": "unknown_platform",
            "task_count": 0,
        }
    remaining = _throttle_remaining(storage, platform)
    if remaining > 0:
        return {
            "platform": platform,
            "ok": False,
            "error": "throttled",
            "message": f"同步过于频繁，请 {remaining} 秒后重试",
            "retry_after": remaining,
            "task_count": 0,
        }
    credential = storage.get_credential(platform)
    if credential is None:
        return {
            "platform": platform,
            "ok": False,
            "error": "credential_not_configured",
            "task_count": 0,
        }
    sync_id = storage.start_sync(platform)
    with sync_lock:
        try:
            adapter = build_adapter(platform, credential)

            def on_rotate(new_credential: str) -> None:
                # refresh 轮换即时落盘：refresh 是一次性凭证，任何后续步骤
                # 失败都不能丢失服务端已下发的新值
                storage.save_credential(platform, new_credential)

            adapter.on_rotate = on_rotate
            known_ids: list[str] = [
                t["external_id"] for t in storage.list_tasks(platform=platform, limit=2000)
            ]
            tasks: list[NormalizedTask] = adapter.fetch_tasks(known_ids=set(known_ids))
            # 双保险：同步结束后仍存在轮换值则再存一次（幂等；即时落盘由 on_rotate 负责）
            rotated = getattr(adapter, "rotated_refresh_token", None)
            if rotated:
                storage.save_credential(platform, "refresh:" + rotated)
                logger.info("persisted rotated refreshToken for %s", platform)
            rotated_jar = getattr(adapter, "rotated_cookie_jar", None)
            if rotated_jar:
                storage.save_credential(platform, "cookies:" + rotated_jar)
                logger.info("persisted rotated cookie jar for %s", platform)
            stats = storage.upsert_tasks(tasks)
            stale_count = storage.mark_stale_except(platform, [t.external_id for t in tasks])
            storage.mark_validation(platform, True)
            message = (
                f"ok inserted={stats['inserted']} updated={stats['updated']} stale={stale_count}"
            )
            storage.finish_sync(sync_id, True, stats["total"], message)
            return {
                "platform": platform,
                "ok": True,
                "task_count": stats["total"],
                "inserted": stats["inserted"],
                "updated": stats["updated"],
                "stale": stale_count,
            }
        except CredentialInvalidError as exc:
            storage.mark_validation(platform, False)
            storage.finish_sync(sync_id, False, 0, f"credential_invalid: {exc}")
            return {
                "platform": platform,
                "ok": False,
                "error": "credential_invalid",
                "message": str(exc),
                "task_count": 0,
            }
        except AdapterError as exc:
            logger.warning("sync %s adapter error: %s", platform, exc)
            storage.finish_sync(sync_id, False, 0, f"adapter_error: {exc}")
            return {
                "platform": platform,
                "ok": False,
                "error": "adapter_error",
                "message": str(exc),
                "task_count": 0,
            }
        except Exception as exc:  # noqa: BLE001 - 同步失败必须落账并返回，不让 API 500
            logger.exception("sync %s unexpected failure", platform)
            storage.finish_sync(sync_id, False, 0, f"unexpected: {exc}")
            return {
                "platform": platform,
                "ok": False,
                "error": "unexpected",
                "message": str(exc),
                "task_count": 0,
            }


def sync_all(storage: AssignmentStorage) -> list[dict[str, Any]]:
    return [sync_platform(storage, p) for p in PLATFORMS]


def due_soon_tasks(storage: AssignmentStorage, within_hours: int = 48) -> list[dict[str, Any]]:
    """Tasks due within `within_hours` and not submitted — feeds the reminder banner."""
    from datetime import datetime, timedelta

    now = datetime.now()
    horizon = int((now + timedelta(hours=within_hours)).timestamp() * 1000)
    out = []
    for t in storage.list_tasks(limit=2000):
        if t["due_at"] is None or t["due_at"] > horizon:
            continue
        if t["status"] in ("submitted", "graded"):
            continue
        out.append(t)
    return out
