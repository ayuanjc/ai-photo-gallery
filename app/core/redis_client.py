"""Redis 异步客户端单例管理。

提供：
- RedisClient：封装 redis.asyncio.Redis，管理连接生命周期。
- connect()：启动时尝试连接（失败只打日志不阻塞启动）。
- aclose()：关闭时释放连接。
- health_check()：PING 探活，返回 bool。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)


class RedisClient:
    """异步 Redis 客户端单例。

    启动时调用 connect()（失败不抛异常），关闭时调用 aclose()。
    通过 health_check() 判断连接是否可用。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis: Any = None
        self._connected: bool = False

    @property
    def redis(self) -> Any:
        """返回底层 redis.asyncio.Redis 实例（未连接时为 None）。"""
        return self._redis

    @property
    def connected(self) -> bool:
        """是否已成功连接。"""
        return self._connected

    async def connect(self) -> None:
        """尝试连接 Redis，失败只打日志不阻塞启动。"""
        if not self._settings.redis_enabled:
            logger.info("Redis 未启用（REDIS_ENABLED=false），跳过连接")
            return

        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self._settings.redis_url,
                socket_timeout=self._settings.redis_timeout_seconds,
                socket_connect_timeout=self._settings.redis_timeout_seconds,
                decode_responses=True,
            )
            await self._redis.ping()
            self._connected = True
            logger.info("Redis 连接成功：url=%s", self._settings.redis_url)
        except Exception as exc:
            logger.warning("Redis 连接失败，将降级使用内存缓存：err=%s", exc)
            self._redis = None
            self._connected = False

    async def aclose(self) -> None:
        """关闭 Redis 连接，释放资源。"""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.warning("Redis 关闭异常：err=%s", exc)
            finally:
                self._redis = None
                self._connected = False

    async def health_check(self) -> bool:
        """PING 探活，返回 True 表示连接正常。"""
        if self._redis is None:
            return False
        try:
            await self._redis.ping()
            self._connected = True
            return True
        except Exception as exc:
            logger.warning("Redis 健康检查失败：err=%s", exc)
            self._connected = False
            return False