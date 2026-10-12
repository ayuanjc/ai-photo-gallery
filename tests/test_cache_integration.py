"""缓存接入集成测试：场景分析缓存 + 搜索结果缓存 + 降级 + 参数隔离。

覆盖：
- 场景分析命中缓存不重复推理
- 搜索命中缓存不调 Immich，cached=true
- 缓存后端异常时降级，接口正常返回
- 不同参数不串缓存
- 降级模式（degraded=true）不写缓存
"""
from __future__ import annotations

import pytest

from app.core.cache import MemoryCache
from app.core.errors import UpstreamUnavailableError
from app.models.request import SearchRequest
from app.scene.analyzer import SceneAnalyzer, SceneInferrer
from app.scene.dictionary import load_scene_dict
from app.services.search_service import SearchService
from app.services.vision.base import VisionProvider, VisionResult


# ---- 测试替身 ----


class FakeInferrer(SceneInferrer):
    name = "fake"

    def __init__(self, scenes=None) -> None:
        self.scenes = list(scenes or [])
        self.calls = 0

    async def infer_scenes(self, asset: dict) -> list[str]:
        self.calls += 1
        return list(self.scenes)


class FakeImmich:
    def __init__(self, assets, raise_on_search=None) -> None:
        self.assets = assets
        self.raise_on_search = raise_on_search
        self.search_calls = 0

    async def smart_search(self, query: str, k: int = 50, asset_ids=None):
        self.search_calls += 1
        if self.raise_on_search:
            raise self.raise_on_search
        return self.assets

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"http://immich.test/api/assets/{asset_id}/original"


class FakeVision(VisionProvider):
    name = "fake"

    def __init__(self, labels=None) -> None:
        self._labels = list(labels or [])

    async def analyze(self, image_url: str) -> VisionResult:
        return VisionResult(labels=list(self._labels))


class BoomCache(MemoryCache):
    """所有操作都抛异常的缓存，用于测试降级。"""

    async def get(self, key: str):
        raise RuntimeError("cache boom")

    async def set(self, key: str, value, ttl: float | None = None) -> None:
        raise RuntimeError("cache boom")

    async def delete(self, key: str) -> bool:
        raise RuntimeError("cache boom")


# ---- 辅助工厂 ----


def _asset(aid: str, score: float) -> dict:
    return {
        "id": aid,
        "score": score,
        "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"},
    }


def _request(**overrides) -> SearchRequest:
    payload = {"query": "海滩", "asset_ids": ["asset-001"]}
    payload.update(overrides)
    return SearchRequest(**payload)


@pytest.fixture
def scene_dict():
    return load_scene_dict()


# =====================================================================
# 任务 1：场景分析缓存
# =====================================================================


async def test_scene_cache_hit_skips_infer(scene_dict):
    """场景分析命中缓存不重复推理。"""
    cache = MemoryCache(maxsize=100, default_ttl=60)
    inferrer = FakeInferrer(["nature"])
    analyzer = SceneAnalyzer(
        scene_dict, inferrer=inferrer, cache=cache, cache_ttl_seconds=60
    )

    asset = {"id": "asset-1"}
    first = await analyzer.analyze(asset)
    second = await analyzer.analyze(asset)

    assert first.primary_scene == "nature"
    assert second.primary_scene == "nature"
    assert inferrer.calls == 1  # 缓存命中，仅推理一次


async def test_scene_cache_miss_different_asset(scene_dict):
    """不同 asset_id 不串缓存，各自推理。"""
    cache = MemoryCache(maxsize=100, default_ttl=60)
    inferrer = FakeInferrer(["nature"])
    analyzer = SceneAnalyzer(
        scene_dict, inferrer=inferrer, cache=cache, cache_ttl_seconds=60
    )

    await analyzer.analyze({"id": "a"})
    await analyzer.analyze({"id": "b"})

    assert inferrer.calls == 2  # 两个不同 asset，各推理一次


async def test_scene_cache_error_degrades_gracefully(scene_dict):
    """缓存后端异常时，分析正常返回，不报错。"""
    cache = BoomCache()
    inferrer = FakeInferrer(["nature"])
    analyzer = SceneAnalyzer(
        scene_dict, inferrer=inferrer, cache=cache, cache_ttl_seconds=60
    )

    tags = await analyzer.analyze({"id": "asset-1"})

    assert tags.primary_scene == "nature"  # 正常返回
    assert inferrer.calls == 1  # 缓存异常，照常推理


async def test_scene_cache_smartinfo_hit_writes_cache(scene_dict):
    """第一级 smartInfo 命中也写缓存，第二次不推理。"""
    cache = MemoryCache(maxsize=100, default_ttl=60)
    inferrer = FakeInferrer()
    analyzer = SceneAnalyzer(
        scene_dict, inferrer=inferrer, cache=cache, cache_ttl_seconds=60
    )

    asset = {"id": "a", "smartInfo": {"tags": ["beach", "sunset"]}}
    first = await analyzer.analyze(asset)
    second = await analyzer.analyze(asset)

    assert first.primary_scene == "nature"
    assert inferrer.calls == 0  # 第一级命中，从不推理
    assert second.primary_scene == "nature"  # 第二次从缓存取


# =====================================================================
# 任务 2：搜索结果缓存
# =====================================================================


async def test_search_cache_hit_skips_immich(settings):
    """搜索命中缓存不调 Immich，cached=true。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision()
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    first = await svc.search(_request())
    second = await svc.search(_request())

    assert first.cached is False
    assert second.cached is True
    assert immich.search_calls == 1  # 缓存命中，仅调 Immich 一次
    assert second.total == first.total
    assert second.results[0].asset_id == first.results[0].asset_id


async def test_search_different_query_no_cross_cache(settings):
    """不同 query 不串缓存，各自调 Immich。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision()
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    await svc.search(_request(query="海滩"))
    await svc.search(_request(query="日落"))

    assert immich.search_calls == 2  # 不同 query，都 miss


async def test_search_different_asset_ids_no_cross_cache(settings):
    """不同 asset_ids 不串缓存。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision()
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    await svc.search(_request(asset_ids=["id-1"]))
    await svc.search(_request(asset_ids=["id-2"]))

    assert immich.search_calls == 2


async def test_search_different_limit_no_cross_cache(settings):
    """不同 limit 不串缓存。"""
    immich = FakeImmich([_asset("a", 0.9), _asset("b", 0.8)])
    vision = FakeVision()
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    await svc.search(_request(limit=1))
    await svc.search(_request(limit=2))

    assert immich.search_calls == 2


async def test_search_degraded_result_not_cached(settings):
    """降级模式（degraded=true）不写缓存，第二次重新调 Immich。"""
    immich = FakeImmich([], raise_on_search=UpstreamUnavailableError("down"))
    vision = FakeVision()
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    first = await svc.search(_request())
    second = await svc.search(_request())

    assert first.degraded is True
    assert second.degraded is True  # 降级结果没缓存，第二次又调 Immich
    assert immich.search_calls == 2


async def test_search_cache_error_degrades_gracefully(settings):
    """缓存后端异常时，搜索正常返回，不报错。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision()
    cache = BoomCache()
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    resp = await svc.search(_request())

    assert resp.total == 1  # 正常返回
    assert resp.cached is False


async def test_search_no_cache_works_as_before(settings):
    """不传 cache 时行为与改造前一致（无缓存，每次调 Immich）。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision()
    svc = SearchService(settings, immich, vision)  # 不传 cache

    first = await svc.search(_request())
    second = await svc.search(_request())

    assert first.cached is False
    assert second.cached is False
    assert immich.search_calls == 2  # 无缓存，每次都调 Immich


async def test_search_cache_key_includes_all_params(settings):
    """缓存 key 包含所有影响结果的参数：权重覆盖也参与 key。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision()
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    await svc.search(_request(time_weight=0.3, similarity_weight=0.7))
    await svc.search(_request(time_weight=0.5, similarity_weight=0.5))

    assert immich.search_calls == 2  # 不同权重，不同 key，都 miss


async def test_search_cache_round_trip_preserves_fields(settings):
    """缓存往返后响应字段完整保留（含 enriched_query、metadata）。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision()
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    first = await svc.search(_request(include_metadata_fields=["width", "city"]))
    second = await svc.search(_request(include_metadata_fields=["width", "city"]))

    assert second.cached is True
    assert second.query == first.query
    assert second.total == first.total
    assert second.enriched_query == first.enriched_query
    assert len(second.results) == len(first.results)
    assert second.results[0].asset_id == first.results[0].asset_id
    assert second.results[0].final_score == first.results[0].final_score

async def test_search_degraded_with_warnings_not_cached(settings):
    """视觉增强产生 warnings（degraded=True）时不写缓存，每次都调 Immich。"""
    immich = FakeImmich([_asset("a", 0.9)])
    vision = FakeVision(labels=["海边"])  # 产生增强 warnings → degraded=True
    cache = MemoryCache(maxsize=100, default_ttl=60)
    svc = SearchService(settings, immich, vision, cache=cache, cache_ttl_seconds=60)

    first = await svc.search(_request())
    second = await svc.search(_request())

    assert first.degraded is True  # 视觉增强产生 warnings
    assert second.cached is False  # degraded=True 不写缓存
    assert immich.search_calls == 2  # 每次都调 Immich