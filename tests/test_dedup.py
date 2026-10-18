"""搜索结果去重测试：dedup_items 单元测试 + SearchService 集成测试。"""
from __future__ import annotations

from datetime import datetime

from app.core.config import Settings
from app.models.request import SearchRequest
from app.services.dedup import dedup_items
from app.services.search_service import SearchService
from app.services.vision.base import VisionProvider, VisionResult

# ============================================================================
# 辅助
# ============================================================================

def _item(
    aid: str,
    final: float = 0.5,
    similarity: float = 0.5,
    captured_at: datetime | None = None,
    width: int | None = 4000,
    height: int | None = 3000,
    exif_time: str | None = None,
    similarity_source: str = "none",
) -> dict:
    """构造 dedup_items 输入项。"""
    exif = {}
    if width is not None:
        exif["exifImageWidth"] = width
    if height is not None:
        exif["exifImageHeight"] = height
    if exif_time is not None:
        exif["dateTimeOriginal"] = exif_time
    return {
        "asset": {"id": aid, "exifInfo": exif},
        "final_score": final,
        "similarity": similarity,
        "similarity_source": similarity_source,
        "immich_rank": 0,
        "captured_at": captured_at,
        "freshness": 0.5,
        "source": "exif",
    }


def _dt(iso: str) -> datetime:
    """从 ISO 字符串构造 datetime。"""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ============================================================================
# 单元测试：dedup_items
# ============================================================================

class TestDedupItems:
    """dedup_items 纯函数测试。"""

    def test_same_asset_id_deduped(self):
        """相同 asset_id：只留一张。"""
        items = [
            _item("a", final=0.9),
            _item("a", final=0.8),
        ]
        result, removed = dedup_items(items)
        assert len(result) == 1
        assert result[0]["final_score"] == 0.9  # 保留高分
        assert removed == 1

    def test_burst_photos_deduped(self):
        """连拍（时间差 1 秒 + 同尺寸）：只留高分那张。"""
        t1 = _dt("2023-06-15T10:00:00+00:00")
        t2 = _dt("2023-06-15T10:00:01+00:00")
        items = [
            _item("a", final=0.9, captured_at=t1),
            _item("b", final=0.8, captured_at=t2),
        ]
        result, removed = dedup_items(items, time_window_sec=3)
        assert len(result) == 1
        assert result[0]["asset"]["id"] == "a"
        assert removed == 1

    def test_time_diff_beyond_window_both_kept(self):
        """时间差超过窗口：都保留。"""
        t1 = _dt("2023-06-15T10:00:00+00:00")
        t2 = _dt("2023-06-15T10:00:10+00:00")
        items = [
            _item("a", final=0.9, captured_at=t1),
            _item("b", final=0.8, captured_at=t2),
        ]
        result, removed = dedup_items(items, time_window_sec=3)
        assert len(result) == 2
        assert removed == 0

    def test_different_dimensions_both_kept(self):
        """时间相近但尺寸不同：都保留。"""
        t1 = _dt("2023-06-15T10:00:00+00:00")
        t2 = _dt("2023-06-15T10:00:01+00:00")
        items = [
            _item("a", final=0.9, captured_at=t1, width=4000, height=3000),
            _item("b", final=0.8, captured_at=t2, width=1920, height=1080),
        ]
        result, removed = dedup_items(items, time_window_sec=3)
        assert len(result) == 2
        assert removed == 0

    def test_high_similarity_same_dims_deduped(self):
        """上游真实相似度超阈值 + 同尺寸：去重。"""
        items = [
            _item("a", final=0.95, similarity=0.98, captured_at=None, similarity_source="upstream"),
            _item("b", final=0.90, similarity=0.97, captured_at=None, similarity_source="upstream"),
        ]
        result, removed = dedup_items(items, similarity_threshold=0.95)
        assert len(result) == 1
        assert removed == 1

    def test_fabricated_similarity_not_deduped(self):
        """非 upstream 相似度（历史名次编分）不得触发去重（R5 回归守卫）。"""
        items = [
            _item("a", final=0.95, similarity=0.98, captured_at=None, similarity_source="none"),
            _item("b", final=0.90, similarity=0.97, captured_at=None, similarity_source="none"),
        ]
        result, removed = dedup_items(items, similarity_threshold=0.95)
        assert len(result) == 2
        assert removed == 0

    def test_below_similarity_threshold_both_kept(self):
        """相似度低于阈值：都保留。"""
        items = [
            _item("a", final=0.9, similarity=0.80, captured_at=None),
            _item("b", final=0.8, similarity=0.70, captured_at=None),
        ]
        result, removed = dedup_items(items, similarity_threshold=0.95)
        assert len(result) == 2
        assert removed == 0

    def test_empty_items(self):
        """空结果：不报错。"""
        result, removed = dedup_items([])
        assert result == []
        assert removed == 0

    def test_single_item(self):
        """单条结果：原样返回。"""
        items = [_item("a", final=0.9)]
        result, removed = dedup_items(items)
        assert len(result) == 1
        assert removed == 0

    def test_dedup_removed_count_correct(self):
        """dedup_removed 数值正确。"""
        t = _dt("2023-06-15T10:00:00+00:00")
        items = [
            _item("a", final=0.9, captured_at=t),
            _item("a", final=0.8, captured_at=t),  # same id → removed
            _item("b", final=0.7, captured_at=t),  # same time+dims → removed
            _item("c", final=0.6, captured_at=_dt("2023-06-15T12:00:00+00:00")),  # kept
        ]
        result, removed = dedup_items(items, time_window_sec=3)
        assert removed == 2
        assert len(result) == 2

    def test_order_preserved(self):
        """返回时保持原有顺序。"""
        t1 = _dt("2023-06-15T10:00:00+00:00")
        t2 = _dt("2023-06-15T12:00:00+00:00")
        t3 = _dt("2023-06-15T14:00:00+00:00")
        items = [
            _item("a", final=0.9, captured_at=t1),
            _item("b", final=0.8, captured_at=t2),
            _item("c", final=0.7, captured_at=t3),
        ]
        result, removed = dedup_items(items, time_window_sec=3)
        assert [r["asset"]["id"] for r in result] == ["a", "b", "c"]

    def test_missing_dimensions_skips_burst_check(self):
        """尺寸缺失：跳过连拍检测，都保留。"""
        t1 = _dt("2023-06-15T10:00:00+00:00")
        t2 = _dt("2023-06-15T10:00:01+00:00")
        items = [
            _item("a", final=0.9, captured_at=t1, width=None, height=None),
            _item("b", final=0.8, captured_at=t2, width=None, height=None),
        ]
        result, removed = dedup_items(items, time_window_sec=3)
        assert len(result) == 2
        assert removed == 0

    def test_none_captured_at_skips_time_check(self):
        """captured_at 为 None：跳过时间检测。"""
        items = [
            _item("a", final=0.9, captured_at=None),
            _item("b", final=0.8, captured_at=None),
        ]
        result, removed = dedup_items(items, time_window_sec=3)
        assert len(result) == 2
        assert removed == 0


# ============================================================================
# 集成测试：通过 SearchService 验证去重接入
# ============================================================================

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
        return VisionResult(labels=[])


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


class TestDedupIntegration:
    """通过 SearchService 验证去重与主流程的交互。"""

    async def test_dedup_enabled_removes_duplicates(self):
        """DEDUP_ENABLED=true：去重生效。"""
        assets = [
            {
                "id": "a",
                "score": 1.0,
                "exifInfo": {
                    "dateTimeOriginal": "2023-06-15T10:00:00Z",
                    "exifImageWidth": 4000,
                    "exifImageHeight": 3000,
                },
            },
            {
                "id": "b",
                "score": 0.99,
                "exifInfo": {
                    "dateTimeOriginal": "2023-06-15T10:00:01Z",
                    "exifImageWidth": 4000,
                    "exifImageHeight": 3000,
                },
            },
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(_settings(), immich, vision)

        resp = await svc.search(SearchRequest(query="test", asset_ids=["x"]))
        assert resp.total == 1
        assert resp.dedup_removed == 1
        assert resp.results[0].asset_id == "a"  # 保留高分

    async def test_dedup_disabled_no_dedup(self):
        """DEDUP_ENABLED=false：不去重。"""
        assets = [
            {
                "id": "a",
                "score": 1.0,
                "exifInfo": {
                    "dateTimeOriginal": "2023-06-15T10:00:00Z",
                    "exifImageWidth": 4000,
                    "exifImageHeight": 3000,
                },
            },
            {
                "id": "b",
                "score": 0.99,
                "exifInfo": {
                    "dateTimeOriginal": "2023-06-15T10:00:01Z",
                    "exifImageWidth": 4000,
                    "exifImageHeight": 3000,
                },
            },
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(_settings(dedup_enabled=False), immich, vision)

        resp = await svc.search(SearchRequest(query="test", asset_ids=["x"]))
        assert resp.total == 2
        assert resp.dedup_removed == 0

    async def test_dedup_removed_field_in_response(self):
        """响应包含 dedup_removed 字段。"""
        immich = FakeImmich([{"id": "a", "score": 1.0, "exifInfo": {}}])
        vision = FakeVision()
        svc = SearchService(_settings(), immich, vision)

        resp = await svc.search(SearchRequest(query="test", asset_ids=["x"]))
        assert hasattr(resp, "dedup_removed")
        assert resp.dedup_removed == 0

    async def test_dedup_failure_degrades_gracefully(self, monkeypatch):
        """dedup_items 抛异常时降级为不去重，不报 5xx。"""
        from app.services import dedup as dedup_mod

        def boom(*args, **kwargs):
            raise RuntimeError("dedup boom")

        monkeypatch.setattr(dedup_mod, "dedup_items", boom)

        assets = [
            {"id": "a", "score": 1.0, "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"}},
            {"id": "b", "score": 0.9, "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"}},
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(_settings(), immich, vision)

        resp = await svc.search(SearchRequest(query="test", asset_ids=["x"]))
        assert resp.total == 2  # 不去重，都保留
        assert resp.dedup_removed == 0
