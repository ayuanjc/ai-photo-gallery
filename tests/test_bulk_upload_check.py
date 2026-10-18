"""bulk-upload-check 测试。"""
from __future__ import annotations

import httpx

from app.services.immich_uploader import ImmichAssetUploader


def make_uploader(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichAssetUploader(settings, client=client), client


async def test_bulk_upload_check_identifies_existing(settings):
    """已存在的资产被标记为 existing，不需要上传。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/assets/bulk-upload-check":
            return httpx.Response(200, json=[
                {"deviceAssetId": "aaa", "deviceId": "dev1"},
            ])
        return httpx.Response(404)

    uploader, client = make_uploader(handler, settings)
    try:
        result = await uploader.bulk_upload_check([
            {"deviceAssetId": "aaa", "deviceId": "dev1"},
            {"deviceAssetId": "bbb", "deviceId": "dev1"},
        ])
        assert "aaa" in result["existing"]
        assert "bbb" in result["new"]
    finally:
        await client.aclose()


async def test_bulk_upload_check_all_new(settings):
    """全部不存在时返回全部为 new。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/assets/bulk-upload-check":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    uploader, client = make_uploader(handler, settings)
    try:
        result = await uploader.bulk_upload_check([
            {"deviceAssetId": "aaa", "deviceId": "dev1"},
            {"deviceAssetId": "bbb", "deviceId": "dev1"},
        ])
        assert result["existing"] == []
        assert set(result["new"]) == {"aaa", "bbb"}
    finally:
        await client.aclose()


async def test_bulk_upload_check_empty_input(settings):
    """空输入返回空结果。"""
    uploader, client = make_uploader(lambda r: httpx.Response(404), settings)
    try:
        result = await uploader.bulk_upload_check([])
        assert result == {"existing": [], "new": []}
    finally:
        await client.aclose()


async def test_bulk_upload_check_failure_degrades(settings):
    """上游失败时降级为全量上传（全部标记为 new）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    uploader, client = make_uploader(handler, settings)
    try:
        result = await uploader.bulk_upload_check([
            {"deviceAssetId": "aaa", "deviceId": "dev1"},
        ])
        assert result["existing"] == []
        assert "aaa" in result["new"]
    finally:
        await client.aclose()
