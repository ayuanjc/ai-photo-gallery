"""图片代理服务测试。"""
from __future__ import annotations

import httpx
import pytest

from app.services.immich import ImmichClient
from app.services.image_proxy import ImageProxyService, ThumbnailFetchError


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


async def test_fetch_thumbnail_success(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/thumbnail" in request.url.path:
            return httpx.Response(
                200,
                content=_PNG_BYTES,
                headers={"content-type": "image/png", "etag": '"abc123"'},
            )
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        svc = ImageProxyService(ic, settings)
        data, ct, etag = await svc.fetch_thumbnail("asset-001", "preview")
        assert data == _PNG_BYTES
        assert ct == "image/png"
        assert etag == '"abc123"'
    finally:
        await client.aclose()


async def test_fetch_thumbnail_invalid_size_falls_back(settings):
    """无效 size 回退到 preview。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/thumbnail" in request.url.path:
            assert request.url.params.get("size") == "preview"
            return httpx.Response(200, content=b"data", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        svc = ImageProxyService(ic, settings)
        data, ct, _ = await svc.fetch_thumbnail("asset-001", "invalid_size")
        assert data == b"data"
    finally:
        await client.aclose()


async def test_fetch_thumbnail_failure(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    ic, client = make_client(handler, settings)
    try:
        svc = ImageProxyService(ic, settings)
        with pytest.raises(ThumbnailFetchError):
            await svc.fetch_thumbnail("nonexistent", "preview")
    finally:
        await client.aclose()


async def test_fetch_original_success(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/original" in request.url.path:
            return httpx.Response(
                200,
                content=b"original_data",
                headers={"content-type": "image/jpeg"},
            )
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        svc = ImageProxyService(ic, settings)
        data, ct, _ = await svc.fetch_original("asset-001")
        assert data == b"original_data"
        assert ct == "image/jpeg"
    finally:
        await client.aclose()


async def test_create_shared_link(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/shared-links" and request.method == "POST":
            return httpx.Response(201, json={"key": "abc123"})
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        svc = ImageProxyService(ic, settings)
        url = await svc.create_shared_link("asset-001")
        assert url is not None
        assert "abc123" in url
    finally:
        await client.aclose()


async def test_create_shared_link_failure(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    ic, client = make_client(handler, settings)
    try:
        svc = ImageProxyService(ic, settings)
        url = await svc.create_shared_link("asset-001")
        assert url is None
    finally:
        await client.aclose()