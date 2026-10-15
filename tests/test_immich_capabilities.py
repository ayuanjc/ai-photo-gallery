"""ImmichCapabilities 能力探测测试（使用 httpx.MockTransport 模拟上游）。"""
from __future__ import annotations

import httpx
import pytest

from app.services.immich import ImmichClient
from app.services.immich_capabilities import ImmichCapabilities


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


async def test_full_capability_detection(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/server/about":
            return httpx.Response(200, json={"info": {"version": "v3.3.1"}})
        if path == "/api/tags":
            return httpx.Response(200, json=[])
        if path == "/api/albums":
            return httpx.Response(200, json=[])
        if path == "/api/people":
            return httpx.Response(200, json={"people": [], "total": 0})
        if path == "/api/search/metadata":
            return httpx.Response(
                200, json={"assets": {"items": []}, "facets": []}
            )
        if path == "/api/search/smart":
            import json as _json
            body = _json.loads(request.content)
            asset_ids = body.get("assetIds", [])
            if "00000000-0000-0000-0000-000000000000" in asset_ids:
                return httpx.Response(
                    200,
                    json={"assets": {"items": [{"id": "00000000-0000-0000-0000-000000000000"}]}},
                )
            return httpx.Response(200, json={"assets": {"items": []}})
        if path == "/api/plugins":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "immich-plugin-core",
                        "version": "2.0.1",
                        "methods": [
                            {"name": "assetAddTags"},
                            {"name": "assetAddToAlbums"},
                            {"name": "webhook"},
                        ],
                    }
                ],
            )
        if path == "/api/workflows":
            return httpx.Response(
                200,
                json=[
                    {"trigger": "AssetCreate"},
                    {"trigger": "AssetTagged"},
                ],
            )
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        cap = await ImmichCapabilities.probe(ic)
        assert cap.version == "v3.3.1"
        assert cap.supports_writeback_tags is True
        assert cap.supports_writeback_albums is True
        assert cap.supports_writeback_people is True
        assert cap.supports_new_search_filters is True
        assert cap.supports_facets is True
        assert cap.supports_asset_ids_in_search is True
        assert cap.supports_plugins is True
        assert cap.supports_workflows is True
        assert "assetAddTags" in cap.plugin_methods
        assert "AssetCreate" in cap.workflow_triggers
        assert cap.probed_at != ""
    finally:
        await client.aclose()


async def test_asset_ids_not_supported(settings):
    """Immich zod 剥离 assetIds 时返回无关资产 → 应标记 False。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/server/about":
            return httpx.Response(200, json={"info": {"version": "v3.3.0"}})
        if path == "/api/tags":
            return httpx.Response(200, json=[])
        if path == "/api/albums":
            return httpx.Response(200, json=[])
        if path == "/api/people":
            return httpx.Response(200, json={"people": []})
        if path == "/api/search/metadata":
            return httpx.Response(200, json={"assets": {"items": []}})
        if path == "/api/search/smart":
            return httpx.Response(
                200,
                json={"assets": {"items": [{"id": "real-asset-123"}]}},
            )
        if path == "/api/plugins":
            return httpx.Response(200, json=[])
        if path == "/api/workflows":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        cap = await ImmichCapabilities.probe(ic)
        assert cap.supports_asset_ids_in_search is False
        assert cap.supports_plugins is False
        assert cap.supports_workflows is False
    finally:
        await client.aclose()


async def test_all_probes_fail_gracefully(settings):
    """所有上游返回 500 → 全部标记 False/None，不抛异常。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    ic, client = make_client(handler, settings)
    try:
        cap = await ImmichCapabilities.probe(ic)
        assert cap.version is None
        assert cap.supports_writeback_tags is False
        assert cap.supports_writeback_albums is False
        assert cap.supports_writeback_people is False
        assert cap.supports_new_search_filters is False
        assert cap.supports_facets is False
        assert cap.supports_asset_ids_in_search is False
        assert cap.supports_plugins is False
        assert cap.supports_workflows is False
        assert cap.plugin_methods == []
        assert cap.workflow_triggers == []
        assert cap.probed_at != ""
    finally:
        await client.aclose()


async def test_as_dict_serializable(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {"version": "v3.3.1"}})

    ic, client = make_client(handler, settings)
    try:
        cap = await ImmichCapabilities.probe(ic)
        d = cap.as_dict()
        assert isinstance(d, dict)
        assert "version" in d
        assert "supports_plugins" in d
        assert isinstance(d["plugin_methods"], list)
    finally:
        await client.aclose()