"""Immich 客户端测试（使用 httpx.MockTransport 模拟上游）。"""
from __future__ import annotations

import httpx
import pytest

from app.core.errors import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError
from app.services.immich import ImmichClient


def make_client(handler, settings, max_retries: int | None = None):
    if max_retries is not None:
        settings = settings.model_copy(update={"immich_max_retries": max_retries})
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


async def test_smart_search_ok(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"assets": {"items": [{"id": "1", "score": 0.8}]}})

    ic, client = make_client(handler, settings)
    try:
        items = await ic.smart_search("海滩")
        assert items == [{"id": "1", "score": 0.8}]
    finally:
        await client.aclose()


async def test_smart_search_no_fabricated_score(settings):
    """上游无 score 时不得按名次编造相似度（R2 回归守卫）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"assets": {"items": [{"id": "1"}, {"id": "2"}]}})

    ic, client = make_client(handler, settings)
    try:
        items = await ic.smart_search("海滩")
        assert items[0].get("score") is None
        assert items[1].get("score") is None
    finally:
        await client.aclose()


async def test_retry_then_success(settings):
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"assets": {"items": []}})

    ic, client = make_client(handler, settings, max_retries=2)
    try:
        await ic.smart_search("海滩")
        assert attempts["count"] == 3
    finally:
        await client.aclose()


async def test_unauthorized_raises_upstream_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    ic, client = make_client(handler, settings, max_retries=0)
    try:
        with pytest.raises(UpstreamError):
            await ic.smart_search("海滩")
    finally:
        await client.aclose()


async def test_connect_error_raises_unavailable(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    ic, client = make_client(handler, settings, max_retries=0)
    try:
        with pytest.raises(UpstreamUnavailableError):
            await ic.smart_search("海滩")
    finally:
        await client.aclose()


async def test_timeout_raises_timeout(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    ic, client = make_client(handler, settings, max_retries=0)
    try:
        with pytest.raises(UpstreamTimeoutError):
            await ic.smart_search("海滩")
    finally:
        await client.aclose()


def test_url_building(settings):
    ic = ImmichClient(settings)
    assert ic.thumbnail_url("42") == "http://immich.test/api/assets/42/thumbnail?size=preview"
    assert ic.original_url("42") == "http://immich.test/api/assets/42/original"
