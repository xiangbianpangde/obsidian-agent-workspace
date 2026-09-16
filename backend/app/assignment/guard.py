"""Outbound SSRF guard for assignment platform requests.

硬性约束（对齐 Mimosa SSRF 规则）：
- 仅允许 http/https（适配器层进一步收紧为 https）
- 发请求前校验 host：平台白名单（*.chaoxing.com / smartestu.cn）
- 解析 DNS 后拒绝环回 / 私有 / 链路本地 / 保留 / 组播地址，防 DNS rebinding
- 30x 跳转由 base adapter 逐跳复验，绝不允许跟随到白名单外
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_HOST_SUFFIXES: tuple[str, ...] = ("chaoxing.com", "smartestu.cn")

# 平台可能在响应里给出 http 链接；服务端出站一律升级 https
_DEFAULT_PORTS = {"https": 443, "http": 80}


class GuardError(Exception):
    """Raised when an outbound URL violates the whitelist / SSRF policy."""


def _host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _assert_public_ips(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise GuardError(f"cannot resolve host {hostname!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise GuardError(f"host {hostname!r} resolves to non-public address {ip}")


def validate_url(url: str) -> str:
    """Validate one outbound URL; returns the normalized https URL or raises."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise GuardError(f"scheme not allowed: {parsed.scheme!r}")
    if not parsed.hostname:
        raise GuardError(f"missing hostname: {url!r}")
    if not _host_allowed(parsed.hostname):
        raise GuardError(f"host not in platform whitelist: {parsed.hostname!r}")
    _assert_public_ips(parsed.hostname)
    scheme = "https"  # 强制升级，避免中间人降级
    netloc = parsed.hostname
    if parsed.port is not None and parsed.port != _DEFAULT_PORTS[scheme]:
        raise GuardError(f"non-default port not allowed: {parsed.port}")
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"
