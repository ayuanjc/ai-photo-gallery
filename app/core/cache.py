"""可插拔缓存后端。

提供：
- CacheBackend：抽象基类，定义 get/set/delete 全异步接口。
- MemoryCache：基于 OrderedDict + TTL 的内存缓存（LRU 淘汰 + 过期清理），
  行为与现有进程内 LRU 缓存一致。
- RedisCache：基于 redis.asyncio 的 Redis 缓存，值为 JSON 字符串。
- get_cache()：工厂函数，根据配置选择后端，Redis 不可用时自动降级到内存。
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings
    from .redis_client import RedisClient

from .redis_client import sanitize_url

logger = logging.getLogger(__name__)


class CacheBackend(ABC):
    """缓存后端抽象基类，全异步接口。"""

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        """取值，不存在返回 None。"""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """存值，ttl 为 None 时用默认存活时间。"""

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """删除 key，返回是否删除成功。"""


class MemoryCache(CacheBackend):
    """内存缓存后端（LRU 淘汰 + TTL 过期）。

    行为与现有进程内 LRU 缓存一致：
    - maxsize 满时淘汰最久未访问的 key（LRU）。
    - ttl 到期自动过期，get 时惰性清理。
    """

    def __init__(self, maxsize: int = 1000, default_ttl: float = 300.0) -> None:
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._expire: dict[str, float] = {}

    async def get(self, key: str) -> Any | None:
        self._purge_expired()
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._purge_expired()
        effective_ttl = self._default_ttl if ttl is None else ttl
        if key in self._data:
            self._data[key] = value
            self._data.move_to_end(key)
        else:
            while len(self._data) >= self._maxsize and self._data:
                evicted_key, _ = self._data.popitem(last=False)
                self._expire.pop(evicted_key, None)
            self._data[key] = value
        self._expire[key] = time.monotonic() + effective_ttl

    async def delete(self, key: str) -> bool:
        self._purge_expired()
        if key in self._data:
            del self._data[key]
            self._expire.pop(key, None)
            return True
        return False

    def _purge_expired(self) -> None:
        """惰性清理过期 key。"""
        now = time.monotonic()
        expired = [k for k, exp in self._expire.items() if exp <= now]
        for k in expired:
            self._data.pop(k, None)
            self._expire.pop(k, None)


class RedisCache(CacheBackend):
    """Redis 缓存后端，值为 JSON 字符串。

    所有 key 自动加前缀（REDIS_KEY_PREFIX），避免与其他服务冲突。
    """

    def __init__(
        self,
        redis: Any,
        key_prefix: str = "imgsearch:",
        default_ttl: float = 300.0,
    ) -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._default_ttl = default_ttl

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> Any | None:
        raw = await self._redis.get(self._full_key(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Redis 缓存反序列化失败：key=%s err=%s", key, exc)
            return None

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        effective_ttl = self._default_ttl if ttl is None else ttl
        payload = json.dumps(value, ensure_ascii=False)
        full_key = self._full_key(key)
        if effective_ttl and effective_ttl > 0:
            await self._redis.set(full_key, payload, px=int(effective_ttl * 1000))
        else:
            await self._redis.set(full_key, payload)

    async def delete(self, key: str) -> bool:
        deleted = await self._redis.delete(self._full_key(key))
        return deleted > 0


async def get_cache(settings: Settings, redis_client: RedisClient) -> CacheBackend:
    """工厂函数：根据配置选择缓存后端。

    选择逻辑：
    - REDIS_ENABLED=true 且 health_check() 通过 → RedisCache
    - 否则 → MemoryCache（降级，不抛异常）

    所有后端共享同一组默认参数：default_ttl=300s，MemoryCache maxsize=1000。
    """
    default_ttl = 300.0

    if settings.redis_enabled and await redis_client.health_check():
        logger.info("缓存后端：RedisCache（url=%s）", sanitize_url(settings.redis_url))
        return RedisCache(
            redis=redis_client.redis,
            key_prefix=settings.redis_key_prefix,
            default_ttl=default_ttl,
        )

    if settings.redis_enabled:
        logger.warning("Redis 已启用但不可用，降级到 MemoryCache")
    else:
        logger.info("缓存后端：MemoryCache（REDIS_ENABLED=false）")
    return MemoryCache(maxsize=1000, default_ttl=default_ttl)
