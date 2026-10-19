"""URL 安全校验：防止 SSRF（服务端请求伪造）。

拒绝向内网/回环/链路本地地址发起请求，仅允许公网 http(s) URL。
DNS 解析失败时 fail-closed（拒绝）。

已知限制（诚实声明）：本模块在"校验时"解析 DNS 并检查全部 A/AAAA 记录，
但无法保证后续实际发起请求的一方（本服务或上游）解析到相同 IP。
若攻击者控制 DNS 且在两次解析之间切换记录（DNS rebinding TOCTOU），
本校验无法完全拦截。当前 image_urls 仅用于提取 asset_id 后调用 Immich
API，本服务不直接向用户提供的 URL 发起请求，实际暴露面有限。
彻底防护需要在请求层 pin IP（自定义 transport），如后续引入本服务侧
下载用户 URL 的功能，必须补齐该层。
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_UNSAFE_HOSTNAMES = frozenset({"localhost", "0.0.0.0", "::1", "[::1]"})

_TRUSTED_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get("URL_TRUSTED_HOSTS", "").split(",")
    if h.strip()
)


def is_safe_url(url: str, *, resolve_dns: bool = True) -> bool:
    """检查 URL 是否安全（非内网、非回环、非链路本地、http/https 协议）。

    - 仅允许 http / https 协议。
    - 拒绝 URL 中包含用户名密码（userinfo）。
    - 拒绝 localhost 等已知不安全主机名。
    - 可信主机白名单（URL_TRUSTED_HOSTS 环境变量）直接放行。
    - IP 地址拒绝私有/回环/链路本地/保留/多播/未分配地址。
    - DNS 解析失败时 fail-closed（返回 False）。

    resolve_dns=False 时跳过 DNS 解析段（Pydantic 同步 validator 场景，
    避免同步 getaddrinfo 阻塞事件循环），域名类主机的 DNS 校验由调用方
    在异步上下文通过 ensure_urls_dns_safe 补齐（fail-closed 不变）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in _UNSAFE_HOSTNAMES:
        return False
    if _TRUSTED_HOSTS and host.lower() in _TRUSTED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not resolve_dns:
            return True
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if not ip.is_global or ip.is_reserved or ip.is_multicast:
                    return False
            return True
        except (socket.gaierror, ValueError):
            return False
    return ip.is_global and not ip.is_reserved and not ip.is_multicast


def validate_url_or_raise(url: str, *, resolve_dns: bool = True) -> str:
    """校验 URL 安全性，不安全时抛 ValueError。"""
    if not is_safe_url(url, resolve_dns=resolve_dns):
        raise ValueError(f"不安全的 URL（可能触发 SSRF）: {url}")
    return url


async def ensure_urls_dns_safe(urls: Iterable[str]) -> None:
    """在异步上下文并发完成 URL 的 DNS 级 SSRF 校验。

    socket.getaddrinfo 是同步阻塞调用，直接在事件循环线程执行会被
    恶意慢 DNS 冻结整个服务；此处通过 asyncio.to_thread 并发校验，
    单请求总耗时约等于最慢一次解析，且不阻塞其他请求。
    任一 URL 不安全时抛 ValueError（fail-closed）。
    """
    targets = [u for u in urls if u]
    if not targets:
        return
    results = await asyncio.gather(
        *(asyncio.to_thread(validate_url_or_raise, u) for u in targets),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
