"""令牌桶限流核心测试。

fakeredis 不支持 Lua eval，故用 FakeTokenBucketRedis（Python 实现等效逻辑）模拟。
覆盖：未超限放行、超限拒绝、令牌随时间补充、补充不超过容量、
Redis 不可用降级、eval 异常降级、开关关闭、不同 key 隔离。
"""
from __future__ import annotations

from app.core.config import Settings
from app.core.rate_limit import TokenBucketRateLimiter


class FakeTokenBucketRedis:
    """模拟 Redis 令牌桶，用 Python 实现与 Lua 脚本等效的逻辑。

    用于测试 RateLimiter 的降级逻辑和令牌桶算法正确性。
    生产环境用真实 Redis 执行 Lua 脚本。
    """

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


def _settings(**kwargs) -> Settings:
    """构造测试用 Settings，默认启用 Redis 和限流。"""
    return Settings(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        redis_enabled=True,
        **kwargs,
    )


# ---- 未超限放行 ----


async def test_allow_first_request():
    """容量 100，首次请求放行，剩余 99。"""
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(_settings(), redis=redis, time_fn=lambda: 1000.0)

    allowed, remaining = await limiter.allow("user:1")

    assert allowed is True
    assert remaining == 99.0


async def test_allow_multiple_under_limit():
    """连续 50 次请求都放行，令牌递减。"""
    redis = FakeTokenBucketRedis()
    t = [1000.0]
    limiter = TokenBucketRateLimiter(_settings(), redis=redis, time_fn=lambda: t[0])

    for i in range(50):
        allowed, remaining = await limiter.allow("user:1")
        assert allowed is True
        assert remaining == 99.0 - i

    assert remaining == 50.0


# ---- 超限拒绝 ----


async def test_deny_over_limit():
    """消耗完令牌后拒绝。"""
    redis = FakeTokenBucketRedis()
    t = [1000.0]
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_burst=5, rate_limit_qps=1),
        redis=redis,
        time_fn=lambda: t[0],
    )

    for _ in range(5):
        allowed, _ = await limiter.allow("user:1")
        assert allowed is True

    allowed, remaining = await limiter.allow("user:1")
    assert allowed is False
    assert remaining < 1.0


async def test_deny_returns_remaining_zero():
    """超限时剩余令牌为 0（或接近 0）。"""
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_burst=1, rate_limit_qps=1),
        redis=redis,
        time_fn=lambda: 1000.0,
    )

    await limiter.allow("user:1")  # 消耗唯一令牌
    allowed, remaining = await limiter.allow("user:1")

    assert allowed is False
    assert remaining == 0.0


# ---- 令牌随时间补充 ----


async def test_token_refill_over_time():
    """消耗令牌后，时间流逝令牌补充。"""
    redis = FakeTokenBucketRedis()
    t = [1000.0]
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_burst=10, rate_limit_qps=10),
        redis=redis,
        time_fn=lambda: t[0],
    )

    for _ in range(10):
        await limiter.allow("user:1")

    allowed, _ = await limiter.allow("user:1")
    assert allowed is False  # 令牌耗尽

    t[0] = 1001.0  # 过 1 秒，补充 10 个（rate=10/s），但不超过 capacity=10
    allowed, remaining = await limiter.allow("user:1")
    assert allowed is True
    assert remaining == 9.0  # 补充到 10，消耗 1，剩 9


async def test_token_refill_capped_at_capacity():
    """令牌补充不超过容量上限。"""
    redis = FakeTokenBucketRedis()
    t = [1000.0]
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_burst=10, rate_limit_qps=10),
        redis=redis,
        time_fn=lambda: t[0],
    )

    for _ in range(5):
        await limiter.allow("user:1")  # 消耗 5 个，剩 5

    t[0] = 1100.0  # 过 100 秒，应补充到 capacity=10（不是 5 + 100*10）
    allowed, remaining = await limiter.allow("user:1")
    assert allowed is True
    assert remaining == 9.0  # 补充到 10，消耗 1，剩 9


async def test_token_partial_refill():
    """部分补充：时间不足以补满。"""
    redis = FakeTokenBucketRedis()
    t = [1000.0]
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_burst=10, rate_limit_qps=20),
        redis=redis,
        time_fn=lambda: t[0],
    )

    for _ in range(10):
        await limiter.allow("user:1")  # 耗尽

    t[0] = 1000.25  # 过 0.25 秒，补充 20*0.25=5 个
    allowed, remaining = await limiter.allow("user:1")
    assert allowed is True
    assert remaining == 4.0  # 补充 5，消耗 1，剩 4


# ---- Redis 不可用降级 ----


async def test_redis_none_always_allow():
    """Redis 实例为 None 时直接放行。"""
    limiter = TokenBucketRateLimiter(_settings(), redis=None)

    for _ in range(200):
        allowed, remaining = await limiter.allow("user:1")
        assert allowed is True
        assert remaining == -1.0


async def test_redis_eval_error_degrades_to_allow():
    """Redis eval 抛异常时放行，不阻塞业务。"""

    class BoomRedis:
        async def eval(self, *args):
            raise RuntimeError("redis down")

    limiter = TokenBucketRateLimiter(_settings(), redis=BoomRedis())

    allowed, remaining = await limiter.allow("user:1")
    assert allowed is True
    assert remaining == -1.0


# ---- 开关关闭 ----


async def test_disabled_always_allow():
    """RATE_LIMIT_ENABLED=false 直接放行，不检查 Redis。"""
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_enabled=False),
        redis=redis,
    )

    for _ in range(200):
        allowed, remaining = await limiter.allow("user:1")
        assert allowed is True
        assert remaining == -1.0


async def test_disabled_ignores_redis_none():
    """开关关闭时即使 Redis 为 None 也放行（双重降级）。"""
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_enabled=False),
        redis=None,
    )

    allowed, remaining = await limiter.allow("user:1")
    assert allowed is True
    assert remaining == -1.0


# ---- 不同 key 隔离 ----


async def test_different_keys_isolated():
    """不同 key 的令牌桶互相独立。"""
    redis = FakeTokenBucketRedis()
    t = [1000.0]
    limiter = TokenBucketRateLimiter(
        _settings(rate_limit_burst=2, rate_limit_qps=1),
        redis=redis,
        time_fn=lambda: t[0],
    )

    await limiter.allow("user:1")
    await limiter.allow("user:1")
    allowed_1, _ = await limiter.allow("user:1")
    assert allowed_1 is False  # user:1 耗尽

    allowed_2, remaining_2 = await limiter.allow("user:2")
    assert allowed_2 is True  # user:2 仍有满容量
    assert remaining_2 == 1.0  # 容量 2，消耗 1，剩 1


# ---- 默认配置 ----


async def test_default_config_qps_50_burst_100():
    """默认配置：QPS=50，容量=100，首次请求剩 99。"""
    redis = FakeTokenBucketRedis()
    limiter = TokenBucketRateLimiter(
        _settings(),
        redis=redis,
        time_fn=lambda: 1000.0,
    )

    allowed, remaining = await limiter.allow("user:1")
    assert allowed is True
    assert remaining == 99.0  # capacity=100，消耗 1
