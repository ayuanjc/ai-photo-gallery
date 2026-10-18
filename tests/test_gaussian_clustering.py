"""高斯核概率软聚类测试。

验证 P(same_person) = exp(-((1 - Sim)^2) / (2 * sigma^2))
在相似度 0.6 和 0.65 时的概率平滑性及聚类行为。
"""
from __future__ import annotations

import math

import pytest

from app.services.face_service import FaceService, calculate_gaussian_probability

# ---- calculate_gaussian_probability 单元测试 ----


def test_gaussian_probability_sim_one_returns_one():
    """Sim=1.0 → P=1.0（完全相同）。"""
    assert calculate_gaussian_probability(1.0, 0.1) == pytest.approx(1.0)


def test_gaussian_probability_monotonic_decreasing():
    """Sim 越低 P 越小（单调递减）。"""
    sims = [1.0, 0.95, 0.9, 0.8, 0.6, 0.3, 0.0]
    probs = [calculate_gaussian_probability(s, 0.1) for s in sims]
    for i in range(len(probs) - 1):
        assert probs[i] > probs[i + 1], f"P({sims[i]})={probs[i]} should > P({sims[i+1]})={probs[i+1]}"


def test_gaussian_probability_smooth_0_6_and_0_65():
    """0.6 和 0.65 的概率平滑过渡（无跳变）。"""
    p_06 = calculate_gaussian_probability(0.6, 0.1)
    p_065 = calculate_gaussian_probability(0.65, 0.1)

    # 两者都是正数
    assert p_06 > 0
    assert p_065 > 0

    # 0.65 的概率高于 0.6（相似度更高 → 概率更高）
    assert p_065 > p_06

    # 概率比值平滑（不会因为 0.01 的差异导致数量级跳变）
    ratio = p_065 / p_06
    assert 1.0 < ratio < 10.0  # 比值合理，非数量级跳变

    # 具体值验证
    expected_06 = math.exp(-((1 - 0.6) ** 2) / (2 * 0.1 ** 2))
    expected_065 = math.exp(-((1 - 0.65) ** 2) / (2 * 0.1 ** 2))
    assert p_06 == pytest.approx(expected_06, rel=1e-9)
    assert p_065 == pytest.approx(expected_065, rel=1e-9)


def test_gaussian_probability_no_hard_cutoff():
    """验证高斯核消除了硬阈值的不连续性。

    硬阈值下 0.61 和 0.63（阈值 0.62）会被完全区分开，
    高斯核下两者概率都是连续的正值，行为一致。
    """
    p_061 = calculate_gaussian_probability(0.61, 0.1)
    p_063 = calculate_gaussian_probability(0.63, 0.1)

    # 两者都是连续的正值（非跳变到 0）
    assert p_061 > 0
    assert p_063 > 0

    # 概率比值合理（非数量级跳变，3 倍以内）
    ratio = p_063 / p_061
    assert 1.0 < ratio < 3.0

    # 两者都不会被 0.85 概率阈值接受（相似度太低）
    assert p_061 < 0.85
    assert p_063 < 0.85


def test_gaussian_probability_sigma_effect():
    """sigma 越大越宽容（概率越高）。"""
    sim = 0.8
    p_strict = calculate_gaussian_probability(sim, 0.05)  # 严格
    p_normal = calculate_gaussian_probability(sim, 0.1)  # 默认
    p_loose = calculate_gaussian_probability(sim, 0.2)  # 宽松

    assert p_strict < p_normal < p_loose


def test_gaussian_probability_sigma_zero_edge_case():
    """sigma=0 时退化 为硬判断：Sim>=1.0 → 1.0，否则 → 0.0。"""
    assert calculate_gaussian_probability(1.0, 0.0) == 1.0
    assert calculate_gaussian_probability(0.99, 0.0) == 0.0
    assert calculate_gaussian_probability(0.5, 0.0) == 0.0


def test_gaussian_probability_clamps_similarity():
    """Sim 超出 [0,1] 被 clamp。"""
    assert calculate_gaussian_probability(1.5, 0.1) == pytest.approx(1.0)
    assert calculate_gaussian_probability(-0.5, 0.1) == pytest.approx(
        math.exp(-(1.0 ** 2) / (2 * 0.1 ** 2))
    )


def test_gaussian_probability_known_values():
    """验证几个关键点的精确值。"""
    # Sim=0.95, sigma=0.1 → exp(-0.125) ≈ 0.8825
    assert calculate_gaussian_probability(0.95, 0.1) == pytest.approx(0.8825, rel=1e-3)

    # Sim=0.9, sigma=0.1 → exp(-0.5) ≈ 0.6065
    assert calculate_gaussian_probability(0.9, 0.1) == pytest.approx(0.6065, rel=1e-3)

    # Sim=0.6, sigma=0.1 → exp(-8) ≈ 0.000335
    assert calculate_gaussian_probability(0.6, 0.1) == pytest.approx(0.000335, rel=1e-2)

    # Sim=0.65, sigma=0.1 → exp(-6.125) ≈ 0.00218
    assert calculate_gaussian_probability(0.65, 0.1) == pytest.approx(0.00218, rel=1e-2)


# ---- 软聚类集成测试 ----


class _FakeFaceClient:
    """集成测试用 FakeFaceClient（计数器确保 face_id 唯一）。"""

    def __init__(self):
        self._counter = 0
        self.search_results: dict[str, list[dict]] = {}

    async def detect_faces(self, url, max_face=10):
        return [{"bounding_box": {"width": 100, "top_left_x": 50, "top_left_y": 50, "height": 120}}]

    async def create_face_set(self, name, capacity=100000):
        return {"face_set_name": name}

    async def delete_face_set(self, name):
        pass

    async def add_face_by_url(self, set_name, url, external_id):
        self._counter += 1
        return f"f{self._counter}"

    async def search_face_by_id(self, set_name, face_id, top_n=10, threshold=0.0):
        return self.search_results.get(face_id, [])

    async def aclose(self):
        pass


async def test_cluster_0_6_and_0_65_both_not_merged(settings):
    """相似度 0.6 和 0.65 在 prob_threshold=0.85 下都不合并（概率太低）。

    但关键是：两者的聚类行为一致（都不会被合并），
    而非硬阈值下 0.6 < 0.62 被拒绝、0.65 > 0.62 被接受的不连续行为。
    """
    from app.models.face import FaceClassifyRequest

    # 相似度 0.6 → P ≈ 0.000335 << 0.85 → 不合并
    fc_low = _FakeFaceClient()
    fc_low.search_results = {
        "f1": [{"face_id": "f2", "similarity": 0.6}],
        "f2": [{"face_id": "f1", "similarity": 0.6}],
    }
    svc = FaceService(settings, fc_low)
    resp_low = await svc.classify(
        FaceClassifyRequest(image_urls=["https://a.com/1.jpg", "https://a.com/2.jpg"])
    )
    assert resp_low.group_count == 2  # 不合并 → 两个独立分组

    # 相似度 0.65 → P ≈ 0.00218 << 0.85 → 也不合并
    fc_mid = _FakeFaceClient()
    fc_mid.search_results = {
        "f1": [{"face_id": "f2", "similarity": 0.65}],
        "f2": [{"face_id": "f1", "similarity": 0.65}],
    }
    svc = FaceService(settings, fc_mid)
    resp_mid = await svc.classify(
        FaceClassifyRequest(image_urls=["https://a.com/1.jpg", "https://a.com/2.jpg"])
    )
    assert resp_mid.group_count == 2  # 也不合并

    # 关键验证：两者行为一致（而非硬阈值下的不连续跳变）
    assert resp_low.group_count == resp_mid.group_count


async def test_cluster_high_similarity_merges(settings):
    """相似度 0.95 → P ≈ 0.8825 > 0.85 → 合并。"""
    from app.models.face import FaceClassifyRequest

    fc = _FakeFaceClient()
    fc.search_results = {
        "f1": [{"face_id": "f2", "similarity": 0.95}],
        "f2": [{"face_id": "f1", "similarity": 0.95}],
    }
    svc = FaceService(settings, fc)
    resp = await svc.classify(
        FaceClassifyRequest(image_urls=["https://a.com/1.jpg", "https://a.com/2.jpg"])
    )
    assert resp.group_count == 1  # 合并
    assert resp.groups[0].face_count == 2


async def test_cluster_probability_threshold_boundary(settings):
    """验证概率阈值边界：P 恰好等于 0.85 时的相似度。

    P(s) = 0.85 → s = 1 - sigma * sqrt(-2 * ln(0.85)) ≈ 1 - 0.1 * 0.5697 ≈ 0.943
    """
    # s_boundary ≈ 0.943 → P ≈ 0.85 → 刚好合并
    s_boundary = 1.0 - 0.1 * math.sqrt(-2 * math.log(0.85))
    assert calculate_gaussian_probability(s_boundary, 0.1) == pytest.approx(0.85, rel=1e-6)

    # 略低于边界 → 不合并
    s_below = s_boundary - 0.01
    assert calculate_gaussian_probability(s_below, 0.1) < 0.85

    # 略高于边界 → 合并
    s_above = s_boundary + 0.01
    assert calculate_gaussian_probability(s_above, 0.1) > 0.85
