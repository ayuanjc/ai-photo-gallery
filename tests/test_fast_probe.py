"""快速连通性检查测试。"""
from __future__ import annotations

import httpx

from app.services.immich import ImmichClient
from app.services.immich_capabilities import ImmichCapabilities


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


async def test_quick_check_success(settings):
    """Immich 可用时，快速检查通过，全量探测正常执行。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/server/ping":
            return httpx.Response(200, json={"res": "pong"})
        if path == "/api/server/about":
            return httpx.Response(200, json={"info": {"version": "v3.3.1"}})
        return httpx.Response(200, json=[])

    ic, client = make_client(handler, settings)
    try:
        cap = await ImmichCapabilities.probe(ic)
        assert cap.version == "v3.3.1"
    finally:
        await client.aclose()


async def test_quick_check_failure_skips_probe(settings):
    """Immich 不可用时，快速检查失败，跳过全量探测，返回空能力集。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    ic, client = make_client(handler, settings)
    try:
        cap = await ImmichCapabilities.probe(ic)
        assert cap.version is None
        assert cap.supports_writeback_tags is False
        assert cap.supports_workflows is False
    finally:
        await client.aclose()


async def test_quick_check_connection_error(settings):
    """连接错误时，快速检查失败，优雅降级。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    ic, client = make_client(handler, settings)
    try:
        cap = await ImmichCapabilities.probe(ic)
        assert cap.version is None
    finally:
        await client.aclose()
