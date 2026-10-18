"""插件 hook 端点测试。"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.scene.fallback import apply_fallback_scene, has_scene_content
from app.scene.models import SceneTags


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

# ---- 兜底场景逻辑单测 ----

class TestHasSceneContent:
    def test_none(self):
        assert has_scene_content(None) is False

    def test_empty_dict(self):
        assert has_scene_content({}) is False

    def test_empty_fields(self):
        assert has_scene_content(
            {"primary_scene": None, "scenes": [], "sub_scenes": [], "visual_attrs": {}}
        ) is False

    def test_has_scenes(self):
        assert has_scene_content({"scenes": ["life"]}) is True

    def test_has_sub_scenes(self):
        assert has_scene_content({"sub_scenes": ["food"]}) is True

    def test_has_visual_attrs(self):
        assert has_scene_content({"visual_attrs": {"time": "day"}}) is True


class TestApplyFallbackScene:
    def test_applies_default_when_empty(self):
        settings = SimpleNamespace(plugin_fallback_scene="life")
        result = apply_fallback_scene(None, settings)
        assert result is not None
        assert result["primary_scene"] == "life"
        assert result["scenes"] == ["life"]
        assert result["confidence"]["life"] == 0.1

    def test_applies_when_analysis_returned_empty(self):
        settings = SimpleNamespace(plugin_fallback_scene="life")
        empty = {"primary_scene": None, "scenes": [], "sub_scenes": [], "visual_attrs": {}}
        result = apply_fallback_scene(empty, settings)
        assert result["primary_scene"] == "life"

    def test_keeps_existing_content(self):
        settings = SimpleNamespace(plugin_fallback_scene="life")
        existing = {"primary_scene": "nature", "scenes": ["nature"], "sub_scenes": [], "visual_attrs": {}}
        assert apply_fallback_scene(existing, settings) is existing

    def test_disabled_when_empty_config(self):
        settings = SimpleNamespace(plugin_fallback_scene="")
        assert apply_fallback_scene(None, settings) is None

    def test_invalid_scene_key_skipped(self):
        settings = SimpleNamespace(plugin_fallback_scene="not_a_scene")
        assert apply_fallback_scene(None, settings) is None

    def test_missing_attr_treated_as_disabled(self):
        settings = SimpleNamespace()
        assert apply_fallback_scene(None, settings) is None


def _make_official_payload(event: str = "AssetCreate") -> dict:
    """构造 Immich v3.x 官方 webhook payload（随机 asset id 避免跨用例 dedup）。"""
    return {
        "type": "AssetV1",
        "trigger": event,
        "data": {"asset": {"id": f"fallback-{uuid.uuid4().hex[:12]}"}},
    }


def test_plugin_hook_desc_writeback_uses_analyzed_scene(client, monkeypatch):
    """真实分析结果（source=analyzed）时，描述/标签写回必须使用现场分析的 scene。"""
    monkeypatch.setattr(
        "app.api.routes.plugin.runtime_enabled", lambda key, default: True
    )
    analyzed = SceneTags(
        primary_scene="nature", scenes=["nature"], sub_scenes=["beach"], source="analyzed"
    )
    analyzer = SimpleNamespace(analyze=AsyncMock(return_value=analyzed))
    monkeypatch.setattr(app.state, "scene_analyzer", analyzer, raising=False)
    mock_desc = AsyncMock()
    mock_desc.writeback_descriptions = AsyncMock(return_value=[])
    mock_scene = AsyncMock()
    mock_scene.writeback_scene_tags = AsyncMock(return_value=[])
    mock_album = AsyncMock()
    mock_album.writeback_albums = AsyncMock(return_value=[])
    monkeypatch.setattr(app.state, "description_writeback", mock_desc, raising=False)
    monkeypatch.setattr(app.state, "scene_writeback", mock_scene, raising=False)
    monkeypatch.setattr(app.state, "album_writeback", mock_album, raising=False)

    resp = client.post("/api/v1/plugin/hook", json=_make_official_payload())
    assert resp.status_code == 200
    assert resp.json()["continue"] is True

    mock_scene.writeback_scene_tags.assert_awaited_once()
    scene_arg = mock_scene.writeback_scene_tags.await_args.args[0][0]
    assert scene_arg["scene"] is not None
    assert scene_arg["scene"]["primary_scene"] == "nature"
    assert scene_arg["scene"]["source"] == "analyzed"

    mock_desc.writeback_descriptions.assert_awaited_once()
    desc_arg = mock_desc.writeback_descriptions.await_args.args[0][0]
    assert desc_arg["scene"] is not None
    assert desc_arg["scene"]["primary_scene"] == "nature"


def test_plugin_hook_no_writeback_for_fallback_scene(client, monkeypatch):
    """回归：webhook payload 无 smartInfo，兜底场景是猜测值，一律不写回。"""
    monkeypatch.setattr(
        "app.api.routes.plugin.runtime_enabled", lambda key, default: True
    )
    mock_desc = AsyncMock()
    mock_desc.writeback_descriptions = AsyncMock(return_value=[])
    mock_scene = AsyncMock()
    mock_scene.writeback_scene_tags = AsyncMock(return_value=[])
    mock_album = AsyncMock()
    mock_album.writeback_albums = AsyncMock(return_value=[])
    monkeypatch.setattr(app.state, "description_writeback", mock_desc, raising=False)
    monkeypatch.setattr(app.state, "scene_writeback", mock_scene, raising=False)
    monkeypatch.setattr(app.state, "album_writeback", mock_album, raising=False)

    resp = client.post("/api/v1/plugin/hook", json=_make_official_payload())
    assert resp.status_code == 200
    assert resp.json()["continue"] is True

    mock_scene.writeback_scene_tags.assert_not_awaited()
    mock_desc.writeback_descriptions.assert_not_awaited()
    mock_album.writeback_albums.assert_not_awaited()


def test_plugin_hook_no_false_rating_without_score(client, monkeypatch):
    """回归：描述写回数据不携带 final_score/similarity_score（排序分不等于图片质量分）。"""
    monkeypatch.setattr(
        "app.api.routes.plugin.runtime_enabled", lambda key, default: True
    )
    analyzed = SceneTags(primary_scene="life", scenes=["life"], source="analyzed")
    analyzer = SimpleNamespace(analyze=AsyncMock(return_value=analyzed))
    monkeypatch.setattr(app.state, "scene_analyzer", analyzer, raising=False)
    mock_desc = AsyncMock()
    mock_desc.writeback_descriptions = AsyncMock(return_value=[])
    monkeypatch.setattr(app.state, "description_writeback", mock_desc, raising=False)
    monkeypatch.setattr(app.state, "scene_writeback", None, raising=False)
    monkeypatch.setattr(app.state, "album_writeback", None, raising=False)

    resp = client.post("/api/v1/plugin/hook", json=_make_official_payload())
    assert resp.status_code == 200
    desc_arg = mock_desc.writeback_descriptions.await_args.args[0][0]
    assert "final_score" not in desc_arg
    assert "similarity_score" not in desc_arg
