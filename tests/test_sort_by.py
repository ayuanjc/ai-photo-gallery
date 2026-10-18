"""排序维度切换（sort_by）单元测试。

覆盖：
- apply_sort_by 函数的四种排序模式
- SearchRequest 的 sort_by 字段校验
- SearchService 端到端集成（sort_by 传递到最终结果）
- SORT_BY_ENABLED=false 时忽略 sort_by
- sort_by 缺省时行为与改动前一致
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.models.request import SearchRequest
from app.services.ranking import apply_sort_by
from app.services.search_service import SearchService
from app.services.vision.base import VisionProvider, VisionResult

# ---- 辅助 ----

def _item(aid: str, captured_at: datetime | None = None, final: float = 0.5) -> dict:
    return {
        "asset": {"id": aid},
        "final_score": final,
        "similarity": 0.5,
        "freshness": 0.5,
        "captured_at": captured_at,
        "source": "exif",
    }


def _dt(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _asset(aid: str, score: float, date: str = "2023-06-15T10:00:00Z") -> dict:
    return {"id": aid, "score": score, "exifInfo": {"dateTimeOriginal": date}}


class _FakeImmich:
    def __init__(self, assets):
        self.assets = assets

    async def smart_search(self, query, k=50, asset_ids=None):
        return self.assets

    def thumbnail_url(self, asset_id, size="preview"):
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id):
        return f"http://immich.test/api/assets/{asset_id}/original"


class _FakeVision(VisionProvider):
    name = "none"

    async def analyze(self, image_url: str) -> VisionResult:
        return VisionResult(labels=[])


def _svc(settings, assets):
    return SearchService(settings, _FakeImmich(assets), _FakeVision())


# ============================================================================
# apply_sort_by 函数测试
# ============================================================================

class TestApplySortBy:
    """直接测试 apply_sort_by 函数。"""

    def test_relevance_preserves_order(self):
        items = [_item("a", _dt(2023)), _item("b", _dt(2024))]
        result = apply_sort_by(items, "relevance")
        assert [i["asset"]["id"] for i in result] == ["a", "b"]

    def test_newest_descending(self):
        items = [
            _item("old", _dt(2020)),
            _item("new", _dt(2024)),
            _item("mid", _dt(2022)),
        ]
        result = apply_sort_by(items, "newest")
        ids = [i["asset"]["id"] for i in result]
        assert ids == ["new", "mid", "old"]

    def test_oldest_ascending(self):
        items = [
            _item("old", _dt(2020)),
            _item("new", _dt(2024)),
            _item("mid", _dt(2022)),
        ]
        result = apply_sort_by(items, "oldest")
        ids = [i["asset"]["id"] for i in result]
        assert ids == ["old", "mid", "new"]

    def test_random_shuffles_but_same_seed_same_result(self):
        items = [_item(f"id{i}", _dt(2020 + i)) for i in range(20)]
        r1 = apply_sort_by(items, "random", random_seed=42)
        r2 = apply_sort_by(items, "random", random_seed=42)
        assert [i["asset"]["id"] for i in r1] == [i["asset"]["id"] for i in r2]

    def test_random_different_seed_different_result(self):
        items = [_item(f"id{i}", _dt(2020 + i)) for i in range(20)]
        r1 = apply_sort_by(items, "random", random_seed=1)
        r2 = apply_sort_by(items, "random", random_seed=2)
        assert [i["asset"]["id"] for i in r1] != [i["asset"]["id"] for i in r2]

    def test_random_preserves_all_items(self):
        items = [_item(f"id{i}", _dt(2020 + i)) for i in range(10)]
        result = apply_sort_by(items, "random", random_seed=99)
        assert len(result) == 10
        assert {i["asset"]["id"] for i in result} == {f"id{i}" for i in range(10)}

    def test_newest_none_captured_at_last(self):
        items = [
            _item("with_time", _dt(2020)),
            _item("no_time", None),
            _item("with_time2", _dt(2024)),
        ]
        result = apply_sort_by(items, "newest")
        ids = [i["asset"]["id"] for i in result]
        assert ids[-1] == "no_time"
        assert ids[0] == "with_time2"

    def test_oldest_none_captured_at_last(self):
        items = [
            _item("with_time", _dt(2020)),
            _item("no_time", None),
            _item("with_time2", _dt(2024)),
        ]
        result = apply_sort_by(items, "oldest")
        ids = [i["asset"]["id"] for i in result]
        assert ids[-1] == "no_time"
        assert ids[0] == "with_time"

    def test_empty_list_no_crash(self):
        assert apply_sort_by([], "relevance") == []
        assert apply_sort_by([], "newest") == []
        assert apply_sort_by([], "oldest") == []
        assert apply_sort_by([], "random") == []

    def test_single_item_all_modes(self):
        items = [_item("only", _dt(2023))]
        for mode in ("relevance", "newest", "oldest", "random"):
            result = apply_sort_by(items, mode)
            assert len(result) == 1
            assert result[0]["asset"]["id"] == "only"

    def test_does_not_mutate_original(self):
        items = [_item("a", _dt(2023)), _item("b", _dt(2024))]
        original = list(items)
        apply_sort_by(items, "newest")
        assert items == original

    def test_unknown_sort_by_returns_copy(self):
        items = [_item("a", _dt(2023))]
        result = apply_sort_by(items, "unknown_mode")
        assert len(result) == 1


# ============================================================================
# SearchRequest 字段校验测试
# ============================================================================

class TestSortByRequest:
    """测试 SearchRequest 的 sort_by 字段。"""

    def test_default_sort_by_is_relevance(self):
        req = SearchRequest(query="test", asset_ids=["a1"])
        assert req.sort_by == "relevance"

    @pytest.mark.parametrize("mode", ["relevance", "newest", "oldest", "random"])
    def test_valid_sort_by_accepted(self, mode):
        req = SearchRequest(query="test", asset_ids=["a1"], sort_by=mode)
        assert req.sort_by == mode

    @pytest.mark.parametrize("invalid", ["", "RELEVANCE", "new", "old", "rand", "score", "1", "null"])
    def test_invalid_sort_by_rejected(self, invalid):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], sort_by=invalid)

    def test_sort_by_type_error_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], sort_by=123)

    def test_sort_by_none_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], sort_by=None)


# ============================================================================
# SearchService 端到端集成测试
# ============================================================================

class TestSortByIntegration:
    """测试 sort_by 从请求到响应的完整链路。"""

    def test_relevance_default_matches_no_sort_by(self, settings):
        """不传 sort_by 与传 sort_by=relevance 结果一致。"""
        assets = [
            _asset("a", 0.9, "2023-01-01T00:00:00Z"),
            _asset("b", 0.8, "2024-01-01T00:00:00Z"),
        ]
        svc = _svc(settings, assets)
        req_default = SearchRequest(query="test", asset_ids=["a1"])
        req_relevance = SearchRequest(query="test", asset_ids=["a1"], sort_by="relevance")

        import asyncio
        resp1 = asyncio.run(svc.search(req_default))
        resp2 = asyncio.run(svc.search(req_relevance))

        ids1 = [r.asset_id for r in resp1.results]
        ids2 = [r.asset_id for r in resp2.results]
        assert ids1 == ids2

    def test_newest_sorts_by_time_descending(self, settings):
        """sort_by=newest → 结果按拍摄时间降序。"""
        assets = [
            _asset("old", 0.9, "2020-01-01T00:00:00Z"),
            _asset("newest", 0.5, "2024-12-31T00:00:00Z"),
            _asset("mid", 0.7, "2022-06-01T00:00:00Z"),
        ]
        svc = _svc(settings, assets)
        req = SearchRequest(query="test", asset_ids=["a1"], sort_by="newest")

        import asyncio
        resp = asyncio.run(svc.search(req))

        ids = [r.asset_id for r in resp.results]
        assert ids == ["newest", "mid", "old"]

    def test_oldest_sorts_by_time_ascending(self, settings):
        """sort_by=oldest → 结果按拍摄时间升序。"""
        assets = [
            _asset("old", 0.9, "2020-01-01T00:00:00Z"),
            _asset("newest", 0.5, "2024-12-31T00:00:00Z"),
            _asset("mid", 0.7, "2022-06-01T00:00:00Z"),
        ]
        svc = _svc(settings, assets)
        req = SearchRequest(query="test", asset_ids=["a1"], sort_by="oldest")

        import asyncio
        resp = asyncio.run(svc.search(req))

        ids = [r.asset_id for r in resp.results]
        assert ids == ["old", "mid", "newest"]

    def test_random_same_seed_same_result(self, settings):
        """sort_by=random + 相同 seed → 相同顺序。"""
        assets = [_asset(f"a{i}", 0.5, f"202{i}-01-01T00:00:00Z") for i in range(10)]
        settings_a = Settings(**{**settings.model_dump(), "sort_by_random_seed": 42})
        settings_b = Settings(**{**settings.model_dump(), "sort_by_random_seed": 42})
        svc_a = _svc(settings_a, assets)
        svc_b = _svc(settings_b, assets)
        req = SearchRequest(query="test", asset_ids=["a1"], sort_by="random")

        import asyncio
        resp_a = asyncio.run(svc_a.search(req))
        resp_b = asyncio.run(svc_b.search(req))

        ids_a = [r.asset_id for r in resp_a.results]
        ids_b = [r.asset_id for r in resp_b.results]
        assert ids_a == ids_b

    def test_random_preserves_all_items(self, settings):
        """sort_by=random → 结果数量不变，元素不变。"""
        assets = [_asset(f"a{i}", 0.5, f"202{i}-01-01T00:00:00Z") for i in range(10)]
        svc = _svc(settings, assets)
        req = SearchRequest(query="test", asset_ids=["a1"], sort_by="random")

        import asyncio
        resp = asyncio.run(svc.search(req))

        ids = {r.asset_id for r in resp.results}
        assert ids == {f"a{i}" for i in range(10)}

    def test_no_captured_at_sorted_last_newest(self, settings):
        """sort_by=newest + 无拍摄时间的资产排最后。"""
        assets = [
            {"id": "with_time", "score": 0.5, "exifInfo": {"dateTimeOriginal": "2023-01-01T00:00:00Z"}},
            {"id": "no_time", "score": 0.5, "exifInfo": {}},
            {"id": "no_exif", "score": 0.5},
        ]
        svc = _svc(settings, assets)
        req = SearchRequest(query="test", asset_ids=["a1"], sort_by="newest")

        import asyncio
        resp = asyncio.run(svc.search(req))

        ids = [r.asset_id for r in resp.results]
        assert ids[0] == "with_time"

    def test_sort_by_disabled_ignores_param(self, settings):
        """SORT_BY_ENABLED=false → 忽略 sort_by，行为与 relevance 一致。"""
        assets = [
            _asset("a", 0.9, "2020-01-01T00:00:00Z"),
            _asset("b", 0.5, "2024-12-31T00:00:00Z"),
        ]
        disabled_settings = Settings(**{**settings.model_dump(), "sort_by_enabled": False})
        svc = _svc(disabled_settings, assets)
        req_newest = SearchRequest(query="test", asset_ids=["a1"], sort_by="newest")
        req_relevance = SearchRequest(query="test", asset_ids=["a1"], sort_by="relevance")

        import asyncio
        resp_newest = asyncio.run(svc.search(req_newest))
        resp_relevance = asyncio.run(svc.search(req_relevance))

        ids_newest = [r.asset_id for r in resp_newest.results]
        ids_relevance = [r.asset_id for r in resp_relevance.results]
        assert ids_newest == ids_relevance

    def test_sort_by_in_cache_key(self, settings):
        """不同 sort_by 产生不同缓存 key（不返回缓存结果）。"""
        from app.core.cache import MemoryCache

        assets = [
            _asset("a", 0.9, "2020-01-01T00:00:00Z"),
            _asset("b", 0.5, "2024-12-31T00:00:00Z"),
        ]
        cache = MemoryCache()
        svc = SearchService(settings, _FakeImmich(assets), _FakeVision(), cache=cache)

        req_relevance = SearchRequest(query="test", asset_ids=["a1"], sort_by="relevance")
        req_newest = SearchRequest(query="test", asset_ids=["a1"], sort_by="newest")

        import asyncio
        resp1 = asyncio.run(svc.search(req_relevance))
        resp2 = asyncio.run(svc.search(req_newest))

        assert resp1.cached is False
        assert resp2.cached is False
