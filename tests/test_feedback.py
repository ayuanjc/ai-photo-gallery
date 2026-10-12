"""搜索反馈闭环服务测试。"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_settings_dep
from app.core.cache import MemoryCache
from app.core.config import Settings
from app.main import app
from app.services.feedback_service import FeedbackService
from app.services.ranking import apply_feedback_boost


def _settings(**overrides) -> Settings:
    defaults = dict(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        feedback_enabled=True,
        feedback_ttl_seconds=604800,
        feedback_boost_max=0.05,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ============================================================================
# FeedbackService 单元测试
# ============================================================================


async def test_click_yields_positive_boost():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    await svc.record_feedback("asset-1", "海边", "click")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost > 0.0
    assert boost == pytest.approx(0.05)


async def test_skip_yields_negative_boost():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    await svc.record_feedback("asset-1", "海边", "skip")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost < 0.0
    assert boost == pytest.approx(-0.05)


async def test_multiple_clicks_proportional_boost():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    for _ in range(3):
        await svc.record_feedback("asset-1", "海边", "click")
    await svc.record_feedback("asset-1", "海边", "skip")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    # (3-1)/(3+1) * 0.05 = 0.025
    assert boost == pytest.approx(0.025)


async def test_boost_never_exceeds_max():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings(feedback_boost_max=0.05))
    for _ in range(100):
        await svc.record_feedback("asset-1", "海边", "click")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost <= 0.05
    assert boost == pytest.approx(0.05)


async def test_boost_never_below_negative_max():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings(feedback_boost_max=0.05))
    for _ in range(100):
        await svc.record_feedback("asset-1", "海边", "skip")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost >= -0.05
    assert boost == pytest.approx(-0.05)


async def test_disabled_returns_zero_boost():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings(feedback_enabled=False))
    await svc.record_feedback("asset-1", "海边", "click")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost == 0.0


async def test_no_feedback_returns_zero():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost == 0.0


async def test_different_queries_isolated():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    await svc.record_feedback("asset-1", "海边", "click")
    boost_a = await svc.get_feedback_boost("asset-1", "海边")
    boost_b = await svc.get_feedback_boost("asset-1", "日落")
    assert boost_a > 0.0
    assert boost_b == 0.0


async def test_different_assets_isolated():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    await svc.record_feedback("asset-1", "海边", "click")
    boost_a = await svc.get_feedback_boost("asset-1", "海边")
    boost_b = await svc.get_feedback_boost("asset-2", "海边")
    assert boost_a > 0.0
    assert boost_b == 0.0


async def test_equal_clicks_and_skips_zero_boost():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    await svc.record_feedback("asset-1", "海边", "click")
    await svc.record_feedback("asset-1", "海边", "skip")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost == 0.0


async def test_invalid_action_ignored():
    cache = MemoryCache()
    svc = FeedbackService(cache, _settings())
    await svc.record_feedback("asset-1", "海边", "invalid")
    boost = await svc.get_feedback_boost("asset-1", "海边")
    assert boost == 0.0


# ============================================================================
# apply_feedback_boost 单元测试
# ============================================================================


def test_boost_zero_unchanged():
    assert apply_feedback_boost(0.5, 0.0) == 0.5


def test_boost_positive_increases():
    assert apply_feedback_boost(0.5, 0.05) == pytest.approx(0.55)


def test_boost_negative_decreases():
    assert apply_feedback_boost(0.5, -0.05) == pytest.approx(0.45)


def test_boost_clamped_to_one():
    assert apply_feedback_boost(0.98, 0.05) == 1.0


def test_boost_clamped_to_zero():
    assert apply_feedback_boost(0.02, -0.05) == 0.0


# ============================================================================
# API 集成测试
# ============================================================================


@contextmanager
def make_client(search_api_key: str = ""):
    app.dependency_overrides[get_settings_dep] = lambda: Settings(
        search_api_key=search_api_key,
        feedback_enabled=True,
        feedback_boost_max=0.05,
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_feedback_record_success():
    with make_client() as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "asset-001", "query": "海边", "action": "click"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["recorded"] is True
    assert body["degraded"] is False


def test_feedback_skip_success():
    with make_client() as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "asset-001", "query": "海边", "action": "skip"},
        )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True


def test_feedback_no_api_key_returns_401():
    with make_client("secret-key") as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "asset-001", "query": "海边", "action": "click"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_feedback_correct_api_key_returns_200():
    with make_client("secret-key") as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "asset-001", "query": "海边", "action": "click"},
            headers={"x-api-key": "secret-key"},
        )
    assert resp.status_code == 200


def test_feedback_invalid_action_returns_400():
    with make_client() as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "asset-001", "query": "海边", "action": "invalid"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAMS"


def test_feedback_empty_asset_id_returns_400():
    with make_client() as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "", "query": "海边", "action": "click"},
        )
    assert resp.status_code == 400


def test_feedback_empty_query_returns_400():
    with make_client() as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "asset-001", "query": "  ", "action": "click"},
        )
    assert resp.status_code == 400


def test_feedback_action_case_insensitive():
    with make_client() as c:
        resp = c.post(
            "/api/v1/search/feedback",
            json={"asset_id": "asset-001", "query": "海边", "action": "CLICK"},
        )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True