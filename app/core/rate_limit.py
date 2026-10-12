"""令牌桶限流核心。

基于 Redis + Lua 脚本实现原子令牌桶：
- capacity = RATE_LIMIT_BURST（桶容量，默认 100）
- rate = RATE_LIMIT_QPS（令牌补充速率，默认 50/秒）
- allow(key) → (放行, 剩余令牌)

降级策略（硬约束：Redis 不可用必须放行）：
- RATE_LIMIT_ENABLED=false → 直接放行，剩余令牌 -1
- Redis 实例为 None → 直接放行，剩余令牌 -1
- eval 抛异常 → 直接放行，剩余令牌 -1
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)

# 令牌桶 Lua 脚本：保证「读取 → 补充 → 判断 → 消耗 → 写回」全流程原子。
# KEYS[1] = 令牌桶 key
# ARGV[1] = capacity（桶容量）
# ARGV[2] = rate（补充速率，令牌/秒）
# ARGV[3] = now（当前时间戳，秒）
# ARGV[4] = cost（本次请求消耗的令牌数，通常 1）
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = now - last_refill
if elapsed > 0 then
    local refill = elapsed * rate
    tokens = tokens + refill
    if tokens > capacity then
        tokens = capacity
    end
    last_refill = now
end

local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'last_refill', last_refill)
local ttl = math.ceil(capacity / rate * 2)
if ttl < 1 then
    ttl = 1
end
redis.call('EXPIRE', key, ttl)

return {allowed, tokens}
"""


class TokenBucketRateLimiter:
    """基于 Redis 的原子令牌桶限流器。

    Redis 不可用或限流关闭时直接放行（降级），不阻塞业务。
    不挂路由，只提供 allow() 方法供上层调用。
    """

    def __init__(
        self,
        settings: Settings,
        redis: Any = None,
        *,
        time_fn: Any = time.time,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._prefix = f"{settings.redis_key_prefix}ratelimit:"
        self._time = time_fn

    async def allow(self, key: str) -> tuple[bool, float]:
        """检查 key 是否放行，返回 (放行, 剩余令牌)。

        - 放行=True 表示允许通过
        - 剩余令牌：放行后桶中剩余令牌数；降级放行时为 -1.0
        """
        if not self._settings.rate_limit_enabled:
            return (True, -1.0)

        if self._redis is None:
            return (True, -1.0)

        try:
            now = self._time()
            full_key = f"{self._prefix}{key}"
            result = await self._redis.eval(
                _LUA_TOKEN_BUCKET,
                1,
                full_key,
                self._settings.rate_limit_burst,
                self._settings.rate_limit_qps,
                now,
                1,
            )
            allowed = bool(int(result[0]))
            remaining = float(result[1])
            return (allowed, remaining)
        except Exception as exc:
            logger.warning("限流检查失败，放行：key=%s err=%s", key, exc)
            return (True, -1.0)