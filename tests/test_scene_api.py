"""场景分析集成测试：搜索响应 scene 字段、失败降级、词典接口。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_search_service
from app.main import app
from app.scene.analyzer import SceneAnalyzer
from app.scene.dictionary import load_scene_dict
from app.services.search_service import SearchService
from app.services.vision.base import VisionProvider, VisionResult


class FakeImmich:
    def __init__(self, assets):
        self.assets = assets

    async def smart_search(self, query: str, k: int = 50, asset_ids=None):
        return self.assets

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"http://immich.test/api/assets/{asset_id}/original"


class FakeVision(VisionProvider):
    name = "fake"

    async def analyze(self, image_url: str) -> VisionResult:
        return VisionResult()


class BoomAnalyzer:
    async def analyze(self, asset: dict):
        raise RuntimeError("scene boom")


def _asset(aid: str, smart_tags=None) -> dict:
    asset = {
        "id": aid,
        "score": 1.0,
        "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"},
    }
    if smart_tags is not None:
        asset["smartInfo"] = {"tags": smart_tags}
    return asset


@pytest.fixture
def normal_client(settings):
    immich = FakeImmich([_asset("a", smart_tags=["beach", "sunset"])])
    svc = SearchService(
        settings,
        immich,
        FakeVision(),
        scene_analyzer=SceneAnalyzer(load_scene_dict()),
    )
    app.dependency_overrides[get_search_service] = lambda: svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def boom_client(settings):
    immich = FakeImmich([_asset("a")])
    svc = SearchService(settings, immich, FakeVision(), scene_analyzer=BoomAnalyzer())
    app.dependency_overrides[get_search_service] = lambda: svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_search_returns_scene_field(normal_client):
    resp = normal_client.post(
        "/api/v1/search/smart",
        json={"query": "海边", "asset_ids": ["asset-001"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    scene = body["results"][0]["scene"]
    assert scene["primary_scene"] == "nature"
    assert "nature" in scene["scenes"]
    assert "beach" in scene["sub_scenes"]
    assert scene["visual_attrs"].get("time") == "sunset"


def test_scene_failure_still_returns_none(boom_client):
    resp = boom_client.post(
        "/api/v1/search/smart",
        json={"query": "海边", "asset_ids": ["asset-001"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["results"][0]["scene"] is None


def test_scene_dict_endpoint():
    with TestClient(app) as c:
        resp = c.get("/api/v1/scene/dict")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["scenes"]) == {
        "nature",
        "city",
        "people",
        "life",
        "event",
        "animal",
        "architecture",
        "night",
        "vehicle",
        "art",
    }
    assert set(body["visual_attrs"]) == {"time", "people", "indoor", "weather"}
    assert body["scenes"]["nature"]["label"] == "自然"