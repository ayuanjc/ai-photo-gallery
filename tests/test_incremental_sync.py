"""增量水位同步服务测试。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.immich import ImmichClient
from app.services.incremental_sync import IncrementalSync


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


async def test_sync_full_no_watermark(settings, tmp_path):
    """无水位时全量拉取。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search/metadata":
            body = json.loads(request.content)
            page = body.get("page", 1)
            if page == 1:
                return httpx.Response(200, json={
                    "assets": {
                        "items": [
                            {"id": "a", "updatedAt": "2026-10-15T10:00:00Z"},
                            {"id": "b", "updatedAt": "2026-10-15T11:00:00Z"},
                        ],
                        "total": 2,
                    }
                })
            return httpx.Response(200, json={"assets": {"items": [], "total": 2}})
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        sync = IncrementalSync(ic, settings, watermark_path=tmp_path / "wm.json")
        result = await sync.sync()
        assert result["total_fetched"] == 2
        assert result["watermark_after"] == "2026-10-15T11:00:00Z"
        assert (tmp_path / "wm.json").exists()
    finally:
        await client.aclose()


async def test_sync_incremental_with_watermark(settings, tmp_path):
    """有水位时增量拉取。"""
    wm_path = tmp_path / "wm.json"
    wm_path.write_text(json.dumps({"updatedAt": "2026-10-14T00:00:00Z"}))

    filter_received = {"filter": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search/metadata":
            body = json.loads(request.content)
            filter_received["filter"] = body.get("filter")
            return httpx.Response(200, json={
                "assets": {
                    "items": [{"id": "c", "updatedAt": "2026-10-15T12:00:00Z"}],
                    "total": 1,
                }
            })
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        sync = IncrementalSync(ic, settings, watermark_path=wm_path)
        result = await sync.sync()
        assert result["total_fetched"] == 1
        assert result["watermark_before"] == "2026-10-14T00:00:00Z"
        assert result["watermark_after"] == "2026-10-15T12:00:00Z"
        assert filter_received["filter"] is not None
        assert "updatedAt" in filter_received["filter"]
    finally:
        await client.aclose()


async def test_sync_callback_called(settings, tmp_path):
    """同步回调被正确调用。"""
    callback = AsyncMock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search/metadata":
            return httpx.Response(200, json={
                "assets": {
                    "items": [{"id": "a", "updatedAt": "2026-10-15T10:00:00Z"}],
                    "total": 1,
                }
            })
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        sync = IncrementalSync(ic, settings, watermark_path=tmp_path / "wm.json")
        await sync.sync(callback=callback)
        assert callback.called
        call_args = callback.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]["id"] == "a"
    finally:
        await client.aclose()


async def test_sync_empty_results(settings, tmp_path):
    """无新数据时返回 0。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search/metadata":
            return httpx.Response(200, json={"assets": {"items": [], "total": 0}})
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        sync = IncrementalSync(ic, settings, watermark_path=tmp_path / "wm.json")
        result = await sync.sync()
        assert result["total_fetched"] == 0
    finally:
        await client.aclose()


async def test_sync_failure_non_blocking(settings, tmp_path):
    """上游失败时返回 0，不抛异常。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    ic, client = make_client(handler, settings)
    try:
        sync = IncrementalSync(ic, settings, watermark_path=tmp_path / "wm.json")
        result = await sync.sync()
        assert result["total_fetched"] == 0
    finally:
        await client.aclose()


async def test_reset_watermark(settings, tmp_path):
    """重置水位删除文件。"""
    wm_path = tmp_path / "wm.json"
    wm_path.write_text(json.dumps({"updatedAt": "2026-10-14T00:00:00Z"}))

    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        sync = IncrementalSync(ic, settings, watermark_path=wm_path)
        assert wm_path.exists()
        sync.reset_watermark()
        assert not wm_path.exists()
    finally:
        await client.aclose()