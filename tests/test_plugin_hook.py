"""插件 hook 端点测试。"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_plugin_hook_no_asset_id(client):
    """无 asset.id 时返回 warning。"""
    resp = client.post("/api/v1/plugin/hook", json={
        "event": "AssetCreate",
        "asset": {},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "缺少 asset.id" in data.get("warnings", [])


def test_plugin_hook_asset_create(client):
    """AssetCreate 事件正常处理。"""
    resp = client.post("/api/v1/plugin/hook", json={
        "event": "AssetCreate",
        "asset": {"id": "test-asset-001"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["continue"] is True


def test_plugin_hook_asset_tagged(client):
    """AssetTagged 事件返回当前标签。"""
    resp = client.post("/api/v1/plugin/hook", json={
        "event": "AssetTagged",
        "asset": {"id": "test-asset-001", "tags": ["AI/自然", "旅行"]},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["changes"].get("current_tags") == ["AI/自然", "旅行"]


def test_plugin_hook_metadata_extraction(client):
    """AssetMetadataExtraction 事件正常处理。"""
    resp = client.post("/api/v1/plugin/hook", json={
        "event": "Asset:MetadataExtraction",
        "asset": {"id": "test-asset-002"},
    })
    assert resp.status_code == 200


def test_plugin_hook_response_format(client):
    """响应格式符合 Immich 期望。"""
    resp = client.post("/api/v1/plugin/hook", json={
        "event": "AssetCreate",
        "asset": {"id": "test-asset-003"},
    })
    data = resp.json()
    assert "changes" in data
    assert "warnings" in data
    assert "continue" in data
    assert isinstance(data["changes"], dict)
    assert isinstance(data["warnings"], list)
    assert isinstance(data["continue"], bool)