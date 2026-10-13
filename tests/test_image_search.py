"""以图搜图功能测试。"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError
from app.models.request import SearchRequest
from app.services.image_query_service import ImageQueryService
from app.services.search_service import SearchService
from app.services.vision.base import VisionProvider, VisionResult


class FakeImmich:
    """假 Immich 客户端，支持 smart_search 和 similar_assets。"""

    def __init__(
        self,
        search_assets: list[dict] | None = None,
        similar_map: dict[str, list[dict]] | None = None,
        raise_on_similar: Exception | None = None,
    ) -> None:
        self._search_assets = search_assets or []
        self._similar_map = similar_map or {}
        self._raise_on_similar = raise_on_similar
        self.smart_search_calls: list[tuple[str, int, list[str] | None]] = []
        self.similar_calls: list[tuple[str, int]] = []

    async def smart_search(self, query: str, k: int = 50, asset_ids=None):
        self.smart_search_calls.append((query, k, asset_ids))
        return self._search_assets

    async def similar_assets(self, asset_id: str, k: int = 50):
        self.similar_calls.append((asset_id, k))
        if self._raise_on_similar:
            raise self._raise_on_similar
        return self._similar_map.get(asset_id, [])

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"http://immich.test/api/assets/{asset_id}/original"


class FakeVision(VisionProvider):
    name = "fake"

    async def analyze(self, image_url: str) -> VisionResult:
        return VisionResult(labels=[])


def _asset(aid: str, score: float = 0.9) -> dict:
    return {"id": aid, "score": score, "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"}}


def _settings(**overrides) -> Settings:
    defaults = dict(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        immich_timeout_seconds=1.0,
        immich_connect_timeout_seconds=1.0,
        immich_max_retries=1,
        immich_retry_backoff_seconds=0.0,
        time_weight=0.3,
        similarity_weight=0.7,
        time_decay_half_life_days=365.0,
        default_datetime_iso="1970-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _img_url(asset_id: str) -> str:
    return f"http://immich.test/api/assets/{asset_id}/original"


# ============================================================================
# ImageQueryService 单元测试
# ============================================================================


class TestExtractAssetId:
    def test_extract_from_original_url(self):
        url = "http://immich.test/api/assets/abc-123/original"
        assert ImageQueryService._extract_asset_id(url) == "abc-123"

    def test_extract_from_thumbnail_url(self):
        url = "http://immich.test/api/assets/xyz-456/thumbnail?size=preview"
        assert ImageQueryService._extract_asset_id(url) == "xyz-456"

    def test_extract_from_bare_path(self):
        url = "http://immich.test/api/assets/asset-789"
        assert ImageQueryService._extract_asset_id(url) == "asset-789"

    def test_extract_returns_none_for_non_immich_url(self):
        url = "http://example.com/photos/123"
        assert ImageQueryService._extract_asset_id(url) is None

    def test_extract_returns_none_for_ssrf_url(self):
        url = "http://127.0.0.1/api/assets/abc/original"
        assert ImageQueryService._extract_asset_id(url) is None

    def test_extract_returns_none_for_localhost(self):
        url = "http://localhost/api/assets/abc/original"
        assert ImageQueryService._extract_asset_id(url) is None

    def test_extract_returns_none_for_empty_id(self):
        url = "http://immich.test/api/assets//original"
        assert ImageQueryService._extract_asset_id(url) is None


class TestResolveAssetIdsFromImages:
    async def test_single_image_returns_similar_ids(self, settings):
        similar_map = {"ref-001": [_asset("sim-a"), _asset("sim-b")]}
        immich = FakeImmich(similar_map=similar_map)
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images([_img_url("ref-001")])

        assert result == {"sim-a", "sim-b"}
        assert warnings == []

    async def test_multiple_images_union_dedup(self, settings):
        similar_map = {
            "ref-001": [_asset("sim-a"), _asset("sim-b")],
            "ref-002": [_asset("sim-b"), _asset("sim-c")],
        }
        immich = FakeImmich(similar_map=similar_map)
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images(
            [_img_url("ref-001"), _img_url("ref-002")]
        )

        assert result == {"sim-a", "sim-b", "sim-c"}
        assert warnings == []

    async def test_similar_failure_skips_image(self, settings):
        immich = FakeImmich(raise_on_similar=UpstreamError("boom"))
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images([_img_url("ref-001")])

        assert result == set()
        assert len(warnings) == 1
        assert "ref-001" in warnings[0]

    async def test_timeout_failure_skips_image(self, settings):
        immich = FakeImmich(raise_on_similar=UpstreamTimeoutError("timeout"))
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images([_img_url("ref-001")])

        assert result == set()
        assert len(warnings) == 1

    async def test_unavailable_failure_skips_image(self, settings):
        immich = FakeImmich(raise_on_similar=UpstreamUnavailableError("down"))
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images([_img_url("ref-001")])

        assert result == set()
        assert len(warnings) == 1

    async def test_non_immich_url_skipped_with_warning(self, settings):
        immich = FakeImmich()
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images(
            ["http://example.com/photos/123"]
        )

        assert result == set()
        assert len(warnings) == 1
        assert "asset_id" in warnings[0]

    async def test_ssrf_url_skipped(self, settings):
        immich = FakeImmich()
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images(
            ["http://127.0.0.1/api/assets/abc/original"]
        )

        assert result == set()
        assert len(warnings) == 1

    async def test_empty_urls_returns_empty(self, settings):
        immich = FakeImmich()
        svc = ImageQueryService(settings, immich)

        result, warnings = await svc.resolve_asset_ids_from_images([])

        assert result == set()
        assert warnings == []

    async def test_partial_failure_returns_partial_results(self, settings):
        similar_map = {"ref-001": [_asset("sim-a")]}
        immich = FakeImmich(similar_map=similar_map, raise_on_similar=None)
        svc = ImageQueryService(settings, immich)

        # First URL succeeds, second URL is non-Immich
        result, warnings = await svc.resolve_asset_ids_from_images(
            [_img_url("ref-001"), "http://example.com/photo/99"]
        )

        assert result == {"sim-a"}
        assert len(warnings) == 1

    async def test_top_k_passed_to_similar_assets(self, settings):
        similar_map = {"ref-001": [_asset("sim-a")]}
        immich = FakeImmich(similar_map=similar_map)
        svc = ImageQueryService(settings, immich)

        await svc.resolve_asset_ids_from_images([_img_url("ref-001")], top_k=25)

        assert immich.similar_calls[0] == ("ref-001", 25)


# ============================================================================
# SearchService 集成测试
# ============================================================================


class TestSearchServiceImageQuery:
    async def test_image_urls_merges_asset_ids(self, settings):
        """带 image_urls 时，相似图 asset_id 合并到搜索候选集。"""
        similar_map = {"ref-001": [_asset("img-sim-1")]}
        immich = FakeImmich(
            search_assets=[_asset("a", 0.9), _asset("b", 1.0)],
            similar_map=similar_map,
        )
        svc = SearchService(settings, immich, FakeVision())

        request = SearchRequest(
            query="海滩",
            asset_ids=["asset-001"],
            image_urls=[_img_url("ref-001")],
        )
        resp = await svc.search(request)

        assert resp.image_query_used is True
        # smart_search 应收到合并后的 asset_ids（包含 ref-001 的相似图）
        _, _, used_asset_ids = immich.smart_search_calls[0]
        assert "asset-001" in used_asset_ids
        assert "img-sim-1" in used_asset_ids

    async def test_image_urls_empty_behavior_unchanged(self, settings):
        """image_urls 为空时行为与改动前完全一致。"""
        immich = FakeImmich(search_assets=[_asset("a", 0.9)])
        svc = SearchService(settings, immich, FakeVision())

        request = SearchRequest(query="海滩", asset_ids=["asset-001"])
        resp = await svc.search(request)

        assert resp.image_query_used is False
        _, _, used_asset_ids = immich.smart_search_calls[0]
        assert used_asset_ids == ["asset-001"]

    async def test_image_query_disabled_behavior_unchanged(self, settings):
        """IMAGE_QUERY_ENABLED=false 时行为与改动前一致。"""
        s = _settings(image_query_enabled=False)
        similar_map = {"ref-001": [_asset("img-sim-1")]}
        immich = FakeImmich(
            search_assets=[_asset("a", 0.9)],
            similar_map=similar_map,
        )
        svc = SearchService(s, immich, FakeVision())

        request = SearchRequest(
            query="海滩",
            asset_ids=["asset-001"],
            image_urls=[_img_url("ref-001")],
        )
        resp = await svc.search(request)

        assert resp.image_query_used is False
        assert immich.similar_calls == []
        _, _, used_asset_ids = immich.smart_search_calls[0]
        assert used_asset_ids == ["asset-001"]

    async def test_similar_failure_degrades_to_text_search(self, settings):
        """取相似图失败时降级为纯文本搜索，接口正常返回。"""
        immich = FakeImmich(
            search_assets=[_asset("a", 0.9)],
            raise_on_similar=UpstreamError("boom"),
        )
        svc = SearchService(settings, immich, FakeVision())

        request = SearchRequest(
            query="海滩",
            asset_ids=["asset-001"],
            image_urls=[_img_url("ref-001")],
        )
        resp = await svc.search(request)

        assert resp.image_query_used is False
        assert resp.total == 1
        assert any("失败" in w or "降级" in w for w in resp.warnings)

    async def test_non_immich_url_degrades_gracefully(self, settings):
        """非 Immich URL 跳过，不报 5xx。"""
        immich = FakeImmich(search_assets=[_asset("a", 0.9)])
        svc = SearchService(settings, immich, FakeVision())

        request = SearchRequest(
            query="海滩",
            asset_ids=["asset-001"],
            image_urls=["http://example.com/photos/123"],
        )
        resp = await svc.search(request)

        assert resp.image_query_used is False
        assert resp.total == 1

    async def test_multiple_image_urls_union(self, settings):
        """多张参考图的相似图取并集。"""
        similar_map = {
            "ref-001": [_asset("sim-a"), _asset("sim-b")],
            "ref-002": [_asset("sim-b"), _asset("sim-c")],
        }
        immich = FakeImmich(
            search_assets=[_asset("a", 0.9)],
            similar_map=similar_map,
        )
        svc = SearchService(settings, immich, FakeVision())

        request = SearchRequest(
            query="海滩",
            asset_ids=["asset-001"],
            image_urls=[_img_url("ref-001"), _img_url("ref-002")],
        )
        resp = await svc.search(request)

        assert resp.image_query_used is True
        _, _, used_asset_ids = immich.smart_search_calls[0]
        assert {"sim-a", "sim-b", "sim-c"}.issubset(set(used_asset_ids))


# ============================================================================
# 请求模型校验测试
# ============================================================================


class TestSearchRequestImageUrls:
    def test_image_urls_default_empty(self):
        req = SearchRequest(query="海滩", asset_ids=["a"])
        assert req.image_urls == []

    def test_image_urls_valid_url(self):
        req = SearchRequest(
            query="海滩",
            asset_ids=["a"],
            image_urls=["http://immich.test/api/assets/abc/original"],
        )
        assert len(req.image_urls) == 1

    def test_image_urls_ssrf_rejected(self):
        with pytest.raises(ValueError, match="不安全"):
            SearchRequest(
                query="海滩",
                asset_ids=["a"],
                image_urls=["http://127.0.0.1/api/assets/abc/original"],
            )

    def test_image_urls_localhost_rejected(self):
        with pytest.raises(ValueError, match="不安全"):
            SearchRequest(
                query="海滩",
                asset_ids=["a"],
                image_urls=["http://localhost/api/assets/abc/original"],
            )

    def test_image_urls_empty_string_rejected(self):
        with pytest.raises(ValueError, match="不能为空"):
            SearchRequest(
                query="海滩",
                asset_ids=["a"],
                image_urls=[""],
            )

    def test_image_urls_duplicate_rejected(self):
        url = "http://immich.test/api/assets/abc/original"
        with pytest.raises(ValueError, match="重复"):
            SearchRequest(
                query="海滩",
                asset_ids=["a"],
                image_urls=[url, url],
            )

    def test_image_urls_max_length_200(self):
        urls = [f"http://immich.test/api/assets/id-{i}/original" for i in range(201)]
        with pytest.raises(Exception):
            SearchRequest(query="海滩", asset_ids=["a"], image_urls=urls)

    def test_image_urls_over_2048_chars_rejected(self):
        long_url = "http://immich.test/api/assets/" + "x" * 2100 + "/original"
        with pytest.raises(ValueError, match="2048"):
            SearchRequest(
                query="海滩",
                asset_ids=["a"],
                image_urls=[long_url],
            )