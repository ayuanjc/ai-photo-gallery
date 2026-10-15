"""搜索结果可解释性测试：build_explanation 单元测试 + SearchService 集成测试。"""
from __future__ import annotations

import math

import pytest

from app.core.errors import VisionError
from app.models.request import SearchRequest
from app.services.explain import build_explanation
from app.services.search_service import SearchService
from app.services.vision.base import VisionProvider, VisionResult


# ============================================================================
# 辅助：复用 test_search_service.py 的 Fake 模式
# ============================================================================

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


# ============================================================================
# 单元测试：build_explanation
# ============================================================================

class TestBuildExplanation:
    """build_explanation 纯函数测试：验证 reasons 生成规则与字段一致性。"""

    def test_high_semantic_and_fresh_time_two_reasons(self):
        """高语义(>=0.8) + 新时间(>=0.7) → reasons 包含两条。"""
        exp = build_explanation({}, 0.85, 0.72, (0.3, 0.7), None)
        assert "语义召回名次靠前" in exp["reasons"]
        assert "拍摄时间较新" in exp["reasons"]
        assert len(exp["reasons"]) == 2

    def test_low_scores_fallback_to综合得分(self):
        """低语义 + 旧时间 → reasons 仅含"综合得分"。"""
        exp = build_explanation({}, 0.3, 0.2, (0.3, 0.7), None)
        assert exp["reasons"] == ["综合得分"]

    def test_matched_scene_includes_chinese_label(self):
        """命中场景 → reasons 包含场景中文名。"""
        scene_tags = {"primary_scene": "nature", "scenes": ["nature"]}
        exp = build_explanation({}, 0.3, 0.2, (0.3, 0.7), scene_tags)
        assert "命中场景：自然" in exp["reasons"]

    def test_matched_scene_city_label(self):
        """命中 city 场景 → reasons 包含"命中场景：城市"。"""
        scene_tags = {"scenes": ["city"]}
        exp = build_explanation({}, 0.3, 0.2, (0.3, 0.7), scene_tags)
        assert "命中场景：城市" in exp["reasons"]

    def test_weights_used_consistent_with_tuple(self):
        """weights 传 tuple → weights_used 与之一致（round 4 位）。"""
        exp = build_explanation({}, 0.5, 0.5, (0.3, 0.7), None)
        assert exp["weights_used"] == {"alpha": 0.3, "beta": 0.7}

    def test_weights_used_consistent_with_dict(self):
        """weights 传 dict → weights_used 与之一致。"""
        exp = build_explanation({}, 0.5, 0.5, {"alpha": 0.4, "beta": 0.6}, None)
        assert exp["weights_used"] == {"alpha": 0.4, "beta": 0.6}

    def test_multiple_matched_scenes_each_gets_reason(self):
        """多个命中场景 → 每个都生成一条 reason。"""
        scene_tags = {"scenes": ["nature", "city"]}
        exp = build_explanation({}, 0.3, 0.2, (0.3, 0.7), scene_tags)
        assert "命中场景：自然" in exp["reasons"]
        assert "命中场景：城市" in exp["reasons"]

    def test_unknown_scene_filtered_from_matched(self):
        """未知场景被过滤，不出现在 matched_scenes。"""
        scene_tags = {"scenes": ["unknown_scene", "nature"]}
        exp = build_explanation({}, 0.3, 0.2, (0.3, 0.7), scene_tags)
        assert exp["matched_scenes"] == ["nature"]

    def test_scores_rounded_to_4_digits(self):
        """分数被 round 到 4 位小数。"""
        exp = build_explanation({}, 0.856789, 0.721234, (0.333, 0.667), None)
        assert exp["semantic_score"] == round(0.856789, 4)
        assert exp["rank_score"] == round(0.856789, 4)
        assert exp["time_score"] == round(0.721234, 4)

    def test_all_reasons_combined(self):
        """高语义 + 新时间 + 命中场景 → 三条 reason 齐全。"""
        scene_tags = {"scenes": ["nature"]}
        exp = build_explanation({}, 0.9, 0.8, (0.2, 0.8), scene_tags)
        assert exp["reasons"] == ["语义召回名次靠前", "拍摄时间较新", "命中场景：自然"]

    def test_scene_tags_none_fallback_to_asset(self):
        """scene_tags 为 None 时回退到 asset 上的 scenes。"""
        asset = {"scenes": ["people"]}
        exp = build_explanation(asset, 0.3, 0.2, (0.3, 0.7), None)
        assert "命中场景：人物" in exp["reasons"]

    def test_empty_scene_tags_no_scene_reason(self):
        """空场景 → 不生成场景 reason，低分时仅"综合得分"。"""
        exp = build_explanation({}, 0.3, 0.2, (0.3, 0.7), {})
        assert exp["reasons"] == ["综合得分"]
        assert exp["matched_scenes"] == []

    def test_structure_has_all_fields(self):
        """返回结构包含所有必需字段。"""
        exp = build_explanation({}, 0.5, 0.5, (0.3, 0.7), None)
        assert "reasons" in exp
        assert "semantic_score" in exp
        assert "rank_score" in exp
        assert "time_score" in exp
        assert "matched_scenes" in exp
        assert "weights_used" in exp


# ============================================================================
# 集成测试：通过 SearchService 验证 explanation 与主流程的交互
# ============================================================================

class TestExplainIntegration:
    """验证 explanation 字段在搜索主流程中的行为。"""

    async def test_explain_enabled_returns_explanation(self, settings):
        """EXPLAIN_ENABLED=true（默认）→ explanation 不为 None。"""
        immich = FakeImmich([_asset("a", 0.9)])
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request())
        assert resp.results[0].explanation is not None
        assert "reasons" in resp.results[0].explanation

    async def test_explain_disabled_returns_none(self, settings):
        """EXPLAIN_ENABLED=false → explanation 为 None。"""
        settings.explain_enabled = False
        immich = FakeImmich([_asset("a", 0.9)])
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request())
        assert resp.results[0].explanation is None

    async def test_explanation_failure_does_not_block_search(
        self, settings, monkeypatch
    ):
        """build_explanation 抛异常时搜索仍正常返回，explanation 为 None。"""

        def boom(*args, **kwargs):
            raise RuntimeError("explain boom")

        monkeypatch.setattr(
            "app.services.search_service.build_explanation", boom
        )
        immich = FakeImmich([_asset("a", 0.9)])
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request())
        assert resp.total == 1
        assert resp.results[0].explanation is None

    async def test_weights_used_match_ranking_blend(self, settings):
        """weights_used 与 ranking.py 的 blend_scene_weights 权重一致。"""
        from app.services.ranking import blend_scene_weights

        asset = {
            "id": "a",
            "score": 0.9,
            "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"},
            "primary_scene": "nature",
            "scenes": ["nature"],
        }
        immich = FakeImmich([asset])
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request())
        exp = resp.results[0].explanation
        assert exp is not None

        # 与 ranking.py 对同一 asset 计算的权重一致
        alpha, beta = blend_scene_weights(
            ["nature"], default_alpha=0.3, default_beta=0.7
        )
        assert exp["weights_used"]["alpha"] == round(alpha, 4)
        assert exp["weights_used"]["beta"] == round(beta, 4)