"""URL 安全校验：防止 SSRF（服务端请求伪造）。

拒绝向内网/回环/链路本地地址发起请求，仅允许公网 http(s) URL。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_UNSAFE_HOSTNAMES = frozenset({"localhost", "0.0.0.0", "::1", "[::1]"})


def is_safe_url(url: str) -> bool:
    """检查 URL 是否安全（非内网、非回环、非链路本地、http/https 协议）。

    - 仅允许 http / https 协议。
    - 拒绝 localhost 等已知不安全主机名（会解析到回环地址）。
    - 域名（非 IP）直接放行，由 DNS 解析后的实际 IP 不在此层校验。
    - IP 地址则拒绝私有/回环/链路本地/保留/多播/未分配地址。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in _UNSAFE_HOSTNAMES:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global and not ip.is_reserved and not ip.is_multicast


def validate_url_or_raise(url: str) -> str:
    """校验 URL 安全性，不安全时抛 ValueError。"""
    if not is_safe_url(url):
        raise ValueError(f"不安全的 URL（可能触发 SSRF）: {url}")
    return url
