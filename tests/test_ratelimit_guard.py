"""搜索接口限流挂载测试。

覆盖：未超限正常返回、超限 429+RATE_LIMITED、X-RateLimit-Remaining 头、
/health 和 /api/v1/scene/dict 不受限流影响、Redis 不可用放行、开关关闭放行、
限流器不存在放行、不同客户端标识隔离、鉴权先于限流。
"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.api.deps import get_search_service, get_settings_dep
from app.core.config import Settings
from app.core.rate_limit import TokenBucketRateLimiter
from app.main import app
from app.models.response import SearchResponse


class FakeTokenBucketRedis:
    """模拟 Redis 令牌桶（同 test_rate_limit.py）。"""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def eval(self, script: str, numkeys: int, *args) -> list:
        key = args[0]
        capacity = float(args[1])
        rate = float(args[2])
        now = float(args[3])
        cost = float(args[4])

        bucket = self._store.get(key)
        if bucket is None:
            tokens = capacity
            last_refill = now
        else:
            tokens = bucket["tokens"]
            last_refill = bucket["last_refill"]

        elapsed = now - last_refill
        if elapsed > 0:
            tokens = min(capacity, tokens + elapsed * rate)
            last_refill = now

        allowed = 0
        if tokens >= cost:
            tokens -= cost
            allowed = 1

        self._store[key] = {"tokens": tokens, "last_refill": last_refill}
        return [allowed, tokens]


class FakeSearchService:
    async def search(self, request):
        return SearchResponse(query=request.query, total=0, results=[])


def _settings(**kwargs) -> Settings:
    defaults = dict(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        redis_enabled=True,
        rate_limit_enabled=True,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


@contextmanager
def make_client(
    *,
    rate_limiter=None,
    settings=None,
):
    """构造测试客户端：注入限流器和假搜索服务。"""
    if settings is None:
        settings = _settings()
    app.dependency_overrides[get_settings_dep] = lambda: settings
    app.dependency_overrides[get_search_service] = lambda: FakeSearchService()
    with TestClient(app) as c:
        app.state.rate_limiter = rate_limiter
        yield c
    app.dependency_overrides.clear()


def _search(client: TestClient, headers=None):
    return client.post(
        "/api/v1/search/smart",
        json={"query": "海边", "asset_ids": ["asset-001"]},
        headers=headers,
    )


# ---- 未超限正常返回 ----


def test_under_limit_returns_200():
    """容量 100，首次请求正常返回 200。"""
    settings = _settings()
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        resp = _search(c)
    assert resp.status_code == 200


# ---- 超限返回 429 ----


def test_over_limit_returns_429():
    """容量 1，第二次请求超限返回 429 + RATE_LIMITED。"""
    settings = _settings(rate_limit_burst=1, rate_limit_qps=1)
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        resp1 = _search(c)
        assert resp1.status_code == 200

        resp2 = _search(c)
        assert resp2.status_code == 429
        body = resp2.json()
        assert body["error"]["code"] == "RATE_LIMITED"


def test_429_has_rate_limit_remaining_header():
    """429 响应带 X-RateLimit-Remaining 头。"""
    settings = _settings(rate_limit_burst=1, rate_limit_qps=1)
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        _search(c)
        resp = _search(c)
        assert resp.status_code == 429
        assert "x-ratelimit-remaining" in resp.headers
        assert resp.headers["x-ratelimit-remaining"] == "0"


# ---- /health 和 /api/v1/scene/dict 不受限流影响 ----


def test_health_not_rate_limited():
    """/health 不挂限流，即使限流器拒绝也正常返回。"""
    settings = _settings(rate_limit_burst=1, rate_limit_qps=1)
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        for _ in range(10):
            resp = c.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}


def test_scene_dict_not_rate_limited():
    """/api/v1/scene/dict 不挂限流。"""
    settings = _settings(rate_limit_burst=1, rate_limit_qps=1)
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        for _ in range(10):
            resp = c.get("/api/v1/scene/dict")
            assert resp.status_code == 200


# ---- Redis 不可用放行 ----


def test_redis_unavailable_passes_through():
    """限流器 Redis 为 None 时降级放行。"""
    settings = _settings()
    limiter = TokenBucketRateLimiter(settings, redis=None)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        for _ in range(200):
            resp = _search(c)
            assert resp.status_code == 200


# ---- 开关关闭放行 ----


def test_rate_limit_disabled_passes_through():
    """RATE_LIMIT_ENABLED=false 时放行。"""
    settings = _settings(rate_limit_enabled=False)
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        for _ in range(200):
            resp = _search(c)
            assert resp.status_code == 200


# ---- 限流器不存在放行 ----


def test_no_limiter_in_state_passes_through():
    """app.state.rate_limiter 为 None 时放行（降级）。"""
    with make_client(rate_limiter=None) as c:
        resp = _search(c)
        assert resp.status_code == 200


# ---- 不同客户端标识隔离 ----


def test_different_ips_isolated():
    """不同 IP 的限流桶互相独立（无 API Key 时按 IP 限流）。"""
    settings = _settings(
        rate_limit_burst=1, rate_limit_qps=1, trusted_proxies=["testclient", "127.0.0.1"]
    )
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        resp_a1 = _search(c, headers={"x-forwarded-for": "1.1.1.1"})
        assert resp_a1.status_code == 200

        resp_a2 = _search(c, headers={"x-forwarded-for": "1.1.1.1"})
        assert resp_a2.status_code == 429

        resp_b1 = _search(c, headers={"x-forwarded-for": "2.2.2.2"})
        assert resp_b1.status_code == 200


# ---- 鉴权先于限流 ----


def test_auth_failure_returns_401_not_429():
    """鉴权失败时返回 401，不检查限流。"""
    settings = _settings(
        search_api_key="secret-key", rate_limit_burst=1, rate_limit_qps=1
    )
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        for _ in range(10):
            resp = _search(c)
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ---- multi-condition 也挂限流 ----


def test_multi_condition_over_limit_returns_429():
    """/api/v1/search/multi-condition 同属 search router，也受限流。"""
    settings = _settings(rate_limit_burst=1, rate_limit_qps=1)
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(settings, redis=redis, time_fn=lambda: 1000.0)
    with make_client(rate_limiter=limiter, settings=settings) as c:
        resp1 = c.post(
            "/api/v1/search/multi-condition",
            json={"query": "海边", "asset_ids": ["asset-001"]},
        )
        assert resp1.status_code == 200

        resp2 = c.post(
            "/api/v1/search/multi-condition",
            json={"query": "海边", "asset_ids": ["asset-001"]},
        )
        assert resp2.status_code == 429
        assert resp2.json()["error"]["code"] == "RATE_LIMITED"