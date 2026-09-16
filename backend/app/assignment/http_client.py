"""Shared HTTP client for assignment platform adapters.

- 所有出站 URL 先过 guard.validate_url()（https 强制 + 白名单 + 公网 IP 校验）
- 绝不跟随重定向（allow_redirects=False），30x 逐跳复验，杜绝跳到白名单外
- 超时上限 15s；连接失败/超时统一转 AdapterError
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from .guard import GuardError, validate_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class AdapterError(Exception):
    """Adapter-level failure (network, auth, parse) surfaced to the API layer."""


class CredentialInvalidError(AdapterError):
    """Platform rejected the stored credential; user must re-import it."""


class BasePlatformClient:
    """Guarded httpx wrapper; subclasses provide platform-specific calls."""

    def __init__(self, headers: dict[str, str] | None = None):
        self._headers: dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if headers:
            self._headers.update(headers)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_hops: int = 3,
        **kwargs: Any,
    ) -> httpx.Response:
        if method.upper() not in ("GET", "POST"):
            raise AdapterError(f"method not allowed by policy: {method}")
        merged = dict(self._headers)
        if headers:
            merged.update(headers)
        current = url
        for _ in range(max_hops + 1):
            try:
                normalized = validate_url(current)
            except GuardError as exc:
                raise AdapterError(f"outbound blocked: {exc}") from exc
            try:
                resp = httpx.request(
                    method,
                    normalized,
                    headers=merged,
                    timeout=DEFAULT_TIMEOUT,
                    follow_redirects=False,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise AdapterError(f"network error: {exc}") from exc
            if resp.is_redirect:
                loc = resp.headers.get("location", "")
                nxt = urljoin(normalized, loc)
                logger.debug("redirect %s -> %s", normalized, nxt)
                current = nxt
                continue
            return resp
        raise AdapterError(f"too many redirects fetching {url}")

    def set_auth_header(self, token: str) -> None:
        """Updates the Authorization header (token rotation support)."""
        self._headers["Authorization"] = f"Bearer {token}"

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)
