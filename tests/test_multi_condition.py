"""复合条件搜索（贝叶斯降级检索）测试。"""
from __future__ import annotations

import pytest

from app.models.request import MultiConditionSearchRequest
from app.services.multi_condition_service import MultiConditionSearchService


class FakeImmich:
    """模拟 Immich 客户端。"""

    def __init__(self):
        self.person_assets: dict[str, list[dict]] = {}
        self.location_assets: dict[str, list[dict]] = {}
        self.query_assets: dict[str, list[dict]] = {}
        self.person_should_fail: set[str] = set()
        self.location_should_fail: set[str] = set()
        self.query_should_fail: set[str] = set()

    async def get_assets_by_person(self, person_id: str) -> list[dict]:
        if person_id in self.person_should_fail:
            raise RuntimeError("person boom")
        return self.person_assets.get(person_id, [])

    async def search_by_location(self, location: str, k: int = 200) -> list[dict]:
        if location in self.location_should_fail:
            raise RuntimeError("location boom")
        return self.location_assets.get(location, [])

    async def smart_search(self, query: str, k: int = 50, asset_ids=None) -> list[dict]:
        if query in self.query_should_fail:
            raise RuntimeError("query boom")
        assets = self.query_assets.get(query, [])
        if asset_ids:
            aid_set = set(asset_ids)
            assets = [a for a in assets if a.get("id") in aid_set]
        return assets

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"


def _asset(aid: str, score: float = 0.9) -> dict:
    return {"id": aid, "score": score}


def _svc(settings, immich=None):
    immich = immich or FakeImmich()
    return MultiConditionSearchService(settings, immich), immich


# ---- 请求模型验证 ----


def test_request_requires_at_least_one_condition():
    with pytest.raises(ValueError, match="至少需要提供一个搜索条件"):
        MultiConditionSearchRequest()


def test_request_with_only_person_id():
    req = MultiConditionSearchRequest(person_id="p1")
    assert req.person_id == "p1"


def test_request_with_all_conditions():
    req = MultiConditionSearchRequest(person_id="p1", location="北京", query="日落")
    assert req.person_id == "p1"
    assert req.location == "北京"
    assert req.query == "日落"


def test_request_query_too_long():
    with pytest.raises(ValueError, match="500"):
        MultiConditionSearchRequest(query="x" * 501)


# ---- 严格模式：三条件同时命中 ----


async def test_strict_mode_all_conditions_match(settings):
    svc, immich = _svc(settings)
    immich.person_assets = {"p1": [_asset("a1", 0.9), _asset("a2", 0.8)]}
    immich.location_assets = {"北京": [_asset("a1", 0.9), _asset("a2", 0.8)]}
    immich.query_assets = {"日落": [_asset("a1", 0.95), _asset("a2", 0.85)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", location="北京", query="日落"))

    assert resp.degraded is False
    assert resp.total == 2
    assert resp.results[0].asset_id == "a1"
    assert "person" in resp.results[0].matched_conditions
    assert "location" in resp.results[0].matched_conditions
    assert "query" in resp.results[0].matched_conditions


async def test_strict_mode_intersection_filters_non_matching(settings):
    svc, immich = _svc(settings)
    immich.person_assets = {"p1": [_asset("a1"), _asset("a2")]}
    immich.location_assets = {"北京": [_asset("a1"), _asset("a3")]}  # a3 不在 person 中
    immich.query_assets = {"日落": [_asset("a1", 0.9)]}  # 只有 a1 在交集中

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", location="北京", query="日落"))

    assert resp.degraded is False
    assert resp.total == 1
    assert resp.results[0].asset_id == "a1"


async def test_strict_mode_no_query_returns_candidates(settings):
    """无 query 时直接返回 person + location 交集候选。"""
    svc, immich = _svc(settings)
    immich.person_assets = {"p1": [_asset("a1", 0.9), _asset("a2", 0.8)]}
    immich.location_assets = {"北京": [_asset("a1", 0.9)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", location="北京"))

    assert resp.degraded is False
    assert resp.total == 1  # 交集只有 a1
    assert resp.results[0].asset_id == "a1"


# ---- 降级模式：严格交集为空 ----


async def test_degraded_mode_triggered_when_strict_empty(settings):
    """严格交集为空时触发降级，分别按条件独立搜索。"""
    svc, immich = _svc(settings)
    # person 有 a1, a2；location 有 a3；query 有 a2, a3
    # 严格交集：person ∩ location = {} → 语义搜索无候选 → 空 → 降级
    immich.person_assets = {"p1": [_asset("a1", 0.9), _asset("a2", 0.8)]}
    immich.location_assets = {"北京": [_asset("a3", 0.7)]}
    immich.query_assets = {"日落": [_asset("a2", 0.85), _asset("a3", 0.75)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", location="北京", query="日落"))

    assert resp.degraded is True
    assert resp.total > 0
    # 降级条件按权重从低到高排序
    assert resp.degraded_conditions == ["query", "location", "person"]


async def test_degraded_mode_weighted_score(settings):
    """验证降级模式加权评分：Score = w1*S_person + w2*S_location + w3*S_query。"""
    svc, immich = _svc(settings)
    # a1 同时出现在 person(0.9) 和 query(0.8) 中
    # Score = 0.5*0.9 + 0.3*0 + 0.2*0.8 = 0.45 + 0.16 = 0.61
    immich.person_assets = {"p1": [_asset("a1", 0.9)]}
    immich.location_assets = {"北京": []}
    immich.query_assets = {"日落": [_asset("a1", 0.8)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", location="北京", query="日落"))

    assert resp.degraded is True
    assert resp.total == 1
    result = resp.results[0]
    assert result.asset_id == "a1"
    assert result.person_score == pytest.approx(0.9)
    assert result.location_score == pytest.approx(0.0)
    assert result.query_score == pytest.approx(0.8)
    assert result.multi_score == pytest.approx(0.5 * 0.9 + 0.3 * 0.0 + 0.2 * 0.8)


async def test_degraded_mode_multi_score_sorting(settings):
    """降级结果按 multi_score 降序排列。"""
    svc, immich = _svc(settings)
    # a1: person=0.9 → score=0.5*0.9=0.45
    # a2: query=1.0 → score=0.2*1.0=0.2
    # a1 应排前面
    immich.person_assets = {"p1": [_asset("a1", 0.9)]}
    immich.query_assets = {"日落": [_asset("a2", 1.0)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", query="日落"))

    assert resp.degraded is True
    assert resp.results[0].asset_id == "a1"
    assert resp.results[0].multi_score > resp.results[1].multi_score


async def test_degraded_mode_high_weight_condition_dominates(settings):
    """高权重条件（person=0.5）的结果应排在低权重条件（query=0.2）前面。"""
    svc, immich = _svc(settings)
    # a1: person=0.6 → score=0.5*0.6=0.30
    # a2: query=0.9 → score=0.2*0.9=0.18
    # 虽然 query 相似度更高，但 person 权重更大 → a1 排前
    immich.person_assets = {"p1": [_asset("a1", 0.6)]}
    immich.query_assets = {"日落": [_asset("a2", 0.9)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", query="日落"))

    assert resp.degraded is True
    assert resp.results[0].asset_id == "a1"  # 高权重条件优先


# ---- 异常降级 ----


async def test_person_filter_failure_does_not_block(settings):
    """人物过滤失败不阻塞，降级继续用其他条件搜索。"""
    svc, immich = _svc(settings)
    immich.person_should_fail = {"p1"}
    immich.query_assets = {"日落": [_asset("a1", 0.9)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", query="日落"))

    assert resp.total >= 1
    assert any("人物过滤失败" in w for w in resp.warnings)


async def test_location_filter_failure_does_not_block(settings):
    """地点过滤失败不阻塞。"""
    svc, immich = _svc(settings)
    immich.location_should_fail = {"北京"}
    immich.query_assets = {"日落": [_asset("a1", 0.9)]}

    resp = await svc.search(MultiConditionSearchRequest(location="北京", query="日落"))

    assert resp.total >= 1
    assert any("地点过滤失败" in w for w in resp.warnings)


async def test_all_conditions_fail_returns_empty(settings):
    """所有条件都失败时返回空结果。"""
    svc, immich = _svc(settings)
    immich.person_should_fail = {"p1"}
    immich.location_should_fail = {"北京"}
    immich.query_should_fail = {"日落"}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", location="北京", query="日落"))

    assert resp.total == 0
    assert resp.degraded is True
    assert any("降级检索仍未找到" in w for w in resp.warnings)


# ---- 单条件搜索 ----


async def test_only_query_search(settings):
    """只有 query 时直接语义搜索。"""
    svc, immich = _svc(settings)
    immich.query_assets = {"日落": [_asset("a1", 0.9), _asset("a2", 0.8)]}

    resp = await svc.search(MultiConditionSearchRequest(query="日落"))

    assert resp.total == 2
    assert resp.degraded is False


async def test_only_person_search(settings):
    """只有 person_id 时返回该人物的所有资产。"""
    svc, immich = _svc(settings)
    immich.person_assets = {"p1": [_asset("a1", 0.9), _asset("a2", 0.8)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1"))

    assert resp.total == 2
    assert resp.degraded is False


# ---- limit 限制 ----


async def test_limit_applied_to_results(settings):
    svc, immich = _svc(settings)
    immich.person_assets = {"p1": [_asset(f"a{i}", 1.0 - i * 0.1) for i in range(10)]}
    immich.query_assets = {"日落": [_asset(f"a{i}", 1.0 - i * 0.1) for i in range(10)]}

    resp = await svc.search(MultiConditionSearchRequest(person_id="p1", query="日落", limit=3))

    assert resp.total <= 3
