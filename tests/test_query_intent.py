"""查询意图动态权重分配模型测试。"""
from __future__ import annotations

import pytest

from app.services.ranking import calculate_dynamic_weights, intent_score

# ---- intent_score 单元测试 ----


def test_intent_score_recent_keywords():
    """包含近期关键词时返回 1.0。"""
    for kw in ("最近", "昨天", "今天", "今年", "上周"):
        assert intent_score(kw) == 1.0


def test_intent_score_recent_in_sentence():
    """近期词嵌入句子中也能识别。"""
    assert intent_score("最近拍的照片") == 1.0
    assert intent_score("今天去公园") == 1.0
    assert intent_score("看看昨天的聚会") == 1.0


def test_intent_score_nostalgic_keywords():
    """包含怀旧关键词时返回 0.0。"""
    for kw in ("以前", "老照片", "童年", "经典"):
        assert intent_score(kw) == 0.0


def test_intent_score_nostalgic_in_sentence():
    """怀旧词嵌入句子中也能识别。"""
    assert intent_score("以前的老照片") == 0.0
    assert intent_score("童年回忆") == 0.0
    assert intent_score("经典风景照") == 0.0


def test_intent_score_neutral_query():
    """无时间意图词时返回 0.5。"""
    assert intent_score("猫") == 0.5
    assert intent_score("海边日落") == 0.5
    assert intent_score("") == 0.5


def test_intent_score_recent_takes_priority_over_nostalgic():
    """同时包含近期和怀旧词时，近期优先（先匹配先返回）。"""
    assert intent_score("最近的老照片") == 1.0


# ---- calculate_dynamic_weights 单元测试 ----


def test_dynamic_weights_recent_query_increases_alpha():
    """近期查询：α_dynamic = α_base + γ * 1.0，时间权重增大。"""
    alpha, beta = calculate_dynamic_weights("最近的照片", 0.3, 0.7, 0.4)
    assert alpha == 0.7  # 0.3 + 0.4 * 1.0
    assert beta == pytest.approx(0.3)
    assert abs(alpha + beta - 1.0) < 1e-9


def test_dynamic_weights_nostalgic_query_keeps_alpha_base():
    """怀旧查询：α_dynamic = α_base + γ * 0.0 = α_base，时间权重不变。"""
    alpha, beta = calculate_dynamic_weights("老照片", 0.3, 0.7, 0.4)
    assert alpha == 0.3
    assert beta == 0.7
    assert abs(alpha + beta - 1.0) < 1e-9


def test_dynamic_weights_neutral_query_midpoint():
    """中性查询：α_dynamic = α_base + γ * 0.5。"""
    alpha, beta = calculate_dynamic_weights("猫", 0.3, 0.7, 0.4)
    assert alpha == 0.5  # 0.3 + 0.4 * 0.5
    assert beta == 0.5
    assert abs(alpha + beta - 1.0) < 1e-9


def test_dynamic_weights_clamp_alpha_to_1():
    """α_dynamic 超过 1 时 clamp 到 1.0。"""
    alpha, beta = calculate_dynamic_weights("最近", 0.8, 0.2, 0.4)
    assert alpha == 1.0  # 0.8 + 0.4 * 1.0 = 1.2 → clamp to 1.0
    assert beta == 0.0


def test_dynamic_weights_clamp_alpha_to_0():
    """α_dynamic 小于 0 时 clamp 到 0.0（base_alpha=0, γ=0, nostalgic → 0）。"""
    alpha, beta = calculate_dynamic_weights("老照片", 0.0, 1.0, 0.4)
    assert alpha == 0.0
    assert beta == 1.0


def test_dynamic_weights_normalizes_base_weights():
    """base_alpha + base_beta ≠ 1 时先归一化。"""
    # base (0.6, 1.4) → normalized (0.3, 0.7) → + 0.4 * 0.5 = 0.5
    alpha, beta = calculate_dynamic_weights("猫", 0.6, 1.4, 0.4)
    assert alpha == 0.5
    assert beta == 0.5


def test_dynamic_weights_zero_base_falls_back():
    """base_alpha + base_beta = 0 时 alpha_norm = 0。"""
    alpha, beta = calculate_dynamic_weights("最近", 0.0, 0.0, 0.4)
    assert alpha == 0.4  # 0 + 0.4 * 1.0
    assert beta == 0.6


def test_dynamic_weights_gamma_zero_is_static():
    """γ=0 时完全静态，等于归一化后的 base 权重。"""
    alpha, beta = calculate_dynamic_weights("最近的照片", 0.3, 0.7, 0.0)
    assert alpha == 0.3
    assert beta == 0.7


def test_dynamic_weights_gamma_full_shift():
    """γ=1.0 时最大动态偏移。"""
    alpha, beta = calculate_dynamic_weights("最近", 0.3, 0.7, 1.0)
    assert alpha == 1.0  # 0.3 + 1.0 * 1.0 = 1.3 → clamp
    assert beta == 0.0

    alpha, beta = calculate_dynamic_weights("老照片", 0.3, 0.7, 1.0)
    assert alpha == 0.3  # 0.3 + 1.0 * 0.0
    assert beta == 0.7

    alpha, beta = calculate_dynamic_weights("猫", 0.3, 0.7, 1.0)
    assert alpha == 0.8  # 0.3 + 1.0 * 0.5
    assert beta == pytest.approx(0.2)


def test_dynamic_weights_sum_always_one():
    """所有情况下 α + β = 1。"""
    for query in ("最近", "老照片", "猫", "昨天", "童年", "海边"):
        for base_alpha in (0.0, 0.3, 0.5, 0.8, 1.0):
            for gamma in (0.0, 0.2, 0.4, 0.6, 1.0):
                alpha, beta = calculate_dynamic_weights(query, base_alpha, 1.0 - base_alpha, gamma)
                assert abs(alpha + beta - 1.0) < 1e-9, f"query={query} alpha={base_alpha} gamma={gamma}"
