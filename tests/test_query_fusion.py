"""多模态查询加权融合测试。"""
from __future__ import annotations

import pytest

from app.services.ranking import fuse_multimodal_query


# ---- 基本融合行为 ----


def test_fuse_default_lambda_repeats_query_twice():
    """λ=0.7 → text_repeat = round(0.7/0.3) = 2，query 重复 2 次。"""
    result = fuse_multimodal_query("海滩", ["海边", "日落"], 0.7)
    assert result == "海滩 海滩 海边 日落"


def test_fuse_lambda_one_returns_only_query():
    """λ=1.0 → 完全使用文本，忽略视觉标签。"""
    result = fuse_multimodal_query("海滩", ["海边", "日落"], 1.0)
    assert result == "海滩"


def test_fuse_lambda_zero_returns_only_vision():
    """λ=0.0 → 完全使用视觉标签。"""
    result = fuse_multimodal_query("海滩", ["海边", "日落"], 0.0)
    assert result == "海边 日落"


def test_fuse_lambda_half_equal_repeat():
    """λ=0.5 → text_repeat = round(1.0) = 1，1:1 等权。"""
    result = fuse_multimodal_query("海滩", ["海边"], 0.5)
    assert result == "海滩 海边"


def test_fuse_lambda_eight_repeats_four_times():
    """λ=0.8 → text_repeat = round(4.0) = 4，query 重复 4 次。"""
    result = fuse_multimodal_query("猫", ["毛茸茸"], 0.8)
    assert result == "猫 猫 猫 猫 毛茸茸"


def test_fuse_lambda_six_repeats_once():
    """λ=0.6 → 0.6/0.4 = 1.4999...（浮点）→ round = 1。"""
    result = fuse_multimodal_query("猫", ["毛茸茸"], 0.6)
    assert result == "猫 毛茸茸"


# ---- 边界条件 ----


def test_fuse_empty_query_returns_vision_tags():
    """query 为空时返回视觉标签。"""
    result = fuse_multimodal_query("", ["海边", "日落"], 0.7)
    assert result == "海边 日落"


def test_fuse_empty_vision_returns_query():
    """视觉标签为空时返回原始 query。"""
    result = fuse_multimodal_query("海滩", [], 0.7)
    assert result == "海滩"


def test_fuse_both_empty_returns_empty():
    """query 和视觉标签都为空时返回空字符串。"""
    result = fuse_multimodal_query("", [], 0.7)
    assert result == ""


def test_fuse_none_inputs_handled():
    """None 输入安全处理。"""
    assert fuse_multimodal_query(None, ["海边"], 0.7) == "海边"  # type: ignore
    assert fuse_multimodal_query("海滩", None, 0.7) == "海滩"  # type: ignore
    assert fuse_multimodal_query(None, None, 0.7) == ""  # type: ignore


# ---- 去重逻辑 ----


def test_fuse_dedup_tags_already_in_query():
    """视觉标签中与 query 重复的词被过滤。"""
    result = fuse_multimodal_query("海滩 日落", ["海滩", "日落", "金色"], 0.5)
    # "海滩" 和 "日落" 已在 query 中 → 只保留 "金色"
    assert result == "海滩 日落 金色"


def test_fuse_dedup_within_vision_tags():
    """视觉标签内部去重。"""
    result = fuse_multimodal_query("猫", ["毛茸茸", "毛茸茸", "可爱"], 0.5)
    assert result == "猫 毛茸茸 可爱"


def test_fue_empty_string_tags_filtered():
    """空字符串标签被过滤。"""
    result = fuse_multimodal_query("猫", ["", "  ", "毛茸茸"], 0.5)
    assert result == "猫 毛茸茸"


# ---- λ clamp ----


def test_fuse_lambda_above_one_clamped():
    """λ > 1 被 clamp 到 1.0，仅返回 query。"""
    result = fuse_multimodal_query("海滩", ["海边"], 1.5)
    assert result == "海滩"


def test_fuse_lambda_below_zero_clamped():
    """λ < 0 被 clamp 到 0.0，仅返回视觉标签。"""
    result = fuse_multimodal_query("海滩", ["海边"], -0.5)
    assert result == "海边"


# ---- 权重比例验证 ----


def test_fuse_text_to_vision_ratio_approximates_lambda():
    """验证重复次数比例近似 λ:(1-λ)。"""
    for lam, expected_repeat in [(0.7, 2), (0.5, 1), (0.8, 4), (0.6, 1), (0.75, 3), (0.9, 9)]:
        result = fuse_multimodal_query("q", ["v"], lam)
        parts = result.split()
        q_count = parts.count("q")
        v_count = parts.count("v")
        assert q_count == expected_repeat, f"λ={lam}: expected {expected_repeat} repeats, got {q_count}"
        assert v_count == 1, f"λ={lam}: expected 1 vision tag, got {v_count}"


# ---- 集成测试：SearchService._enrich_query 使用 fuse_multimodal_query ----


def test_enrich_query_uses_fuse_multimodal():
    """集成：_enrich_query 应使用 fuse_multimodal_query 而非简单拼接。"""
    from app.core.config import Settings
    from app.models.request import SearchRequest
    from app.services.search_service import SearchService
    from app.services.vision.base import VisionProvider, VisionResult

    class FakeImmich:
        async def smart_search(self, query, k=50, asset_ids=None):
            return []

        def thumbnail_url(self, asset_id, size="preview"):
            return f"http://test/{asset_id}"

        def original_url(self, asset_id):
            return f"http://test/{asset_id}"

    class FakeVision(VisionProvider):
        name = "fake"

        async def analyze(self, image_url):
            return VisionResult(labels=["海边", "日落"])

    settings = Settings(
        immich_api_key="test",
        multimodal_lambda=0.7,
        search_api_key="",
    )
    svc = SearchService(settings, FakeImmich(), FakeVision())
    request = SearchRequest(query="海滩", asset_ids=["a1"])

    import asyncio
    enriched, warnings = asyncio.run(svc._enrich_query(request))

    # λ=0.7 → "海滩" 重复 2 次 + "海边 日落"
    assert enriched == "海滩 海滩 海边 日落"
    assert len(warnings) == 1
    assert "2" in warnings[0]  # 2 个标签