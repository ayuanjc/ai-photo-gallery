"""搜索编排服务测试（注入假 Immich / 假视觉识别）。"""
from __future__ import annotations

from app.core.errors import VisionError
from app.models.request import SearchRequest
from app.services.search_service import SearchService
from app.services.immich_capabilities import ImmichCapabilities
from app.services.vision.base import VisionProvider, VisionResult


class FakeImmich:
    def __init__(self, assets, raise_on_search=None):
        self.assets = assets
        self.raise_on_search = raise_on_search
        self.queries: list[tuple[str, int]] = []

    async def smart_search(self, query: str, k: int = 50, asset_ids=None):
        self.queries.append((query, k))
        if self.raise_on_search:
            raise self.raise_on_search
        return self.assets

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"http://immich.test/api/assets/{asset_id}/original"


class FakeVision(VisionProvider):
    name = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    async def analyze(self, image_url: str) -> VisionResult:
        self.calls.append(image_url)
        if self.fail:
            raise VisionError("vision boom")
        return VisionResult(labels=["海边", "日落"])


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


async def test_search_returns_ranked_results(settings):
    immich = FakeImmich([_asset("a", 0.9), _asset("b", 1.0)])
    vision = FakeVision()
    svc = SearchService(settings, immich, vision)

    resp = await svc.search(_request())

    assert resp.total == 2
    assert resp.results[0].asset_id == "b"  # 相似度更高且时间相同
    assert resp.enriched_query is not None  # 被视觉标签增强
    assert "海边" in resp.enriched_query


async def test_vision_failure_degrades_not_blocks(settings):
    immich = FakeImmich([_asset("a", 1.0)])
    vision = FakeVision(fail=True)
    svc = SearchService(settings, immich, vision)

    resp = await svc.search(_request())

    assert resp.degraded is False  # 视觉失败仅记录 warning，不算降级
    assert any("视觉" in w or "标签" in w for w in resp.warnings) or resp.enriched_query is None
    assert resp.total == 1


async def test_request_weight_override(settings):
    immich = FakeImmich([_asset("a", 1.0)])
    vision = FakeVision()
    svc = SearchService(settings, immich, vision)

    req = _request(time_weight=1.0, similarity_weight=0.0)
    resp = await svc.search(req)
    assert resp.total == 1
    # 权重全给时间，相似度不影响最终分数公式（此处仅验证不报错且归一化正确）
    assert 0.0 <= resp.results[0].final_score <= 1.0


async def test_metadata_filtering(settings):
    immich = FakeImmich(
        [
            {
                "id": "a",
                "score": 1.0,
                "exifInfo": {
                    "dateTimeOriginal": "2023-06-15T10:00:00Z",
                    "exifImageWidth": 4000,
                    "city": "三亚",
                },
            }
        ]
    )
    vision = FakeVision()
    svc = SearchService(settings, immich, vision)

    resp = await svc.search(_request(include_metadata_fields=["width", "city"]))
    meta = resp.results[0].metadata
    assert "width" in meta
    assert "city" in meta
    assert "format" not in meta

async def test_asset_ids_local_filter_when_unsupported(settings):
    """Immich 不支持 assetIds 时，本地交集过滤搜索结果。"""
    immich = FakeImmich([_asset("a", 0.9), _asset("b", 0.8), _asset("c", 0.7)])
    vision = FakeVision()
    cap = ImmichCapabilities(supports_asset_ids_in_search=False)
    svc = SearchService(settings, immich, vision, capabilities=cap)

    resp = await svc.search(_request(asset_ids=["a", "c"]))
    ids = {r.asset_id for r in resp.results}
    assert ids == {"a", "c"}


async def test_asset_ids_no_filter_when_supported(settings):
    """Immich 支持 assetIds 时，不过滤（信任上游已过滤）。"""
    immich = FakeImmich([_asset("a", 0.9), _asset("b", 0.8)])
    vision = FakeVision()
    cap = ImmichCapabilities(supports_asset_ids_in_search=True)
    svc = SearchService(settings, immich, vision, capabilities=cap)

    resp = await svc.search(_request(asset_ids=["a"]))
    ids = {r.asset_id for r in resp.results}
    assert ids == {"a", "b"}


async def test_asset_ids_no_filter_when_no_capabilities(settings):
    """无 capabilities（None）时，不过滤（向后兼容）。"""
    immich = FakeImmich([_asset("a", 0.9), _asset("b", 0.8)])
    vision = FakeVision()
    svc = SearchService(settings, immich, vision)

    resp = await svc.search(_request(asset_ids=["a"]))
    assert resp.total == 2