"""基于时间衰减的 LFU 缓存热度模型测试。

Heat = Freq * (2 ^ (-Age / HalfLife))

后半部分为可插拔缓存后端（MemoryCache / RedisCache / 工厂降级）测试。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.core.cache import MemoryCache, RedisCache, get_cache
from app.core.config import Settings
from app.core.redis_client import RedisClient
from app.utils.cache import TimeDecayedLFUCache

# ---- 基本操作 ----


def test_basic_set_get():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    assert cache["a"] == 1
    assert len(cache) == 1


def test_overwrite_existing_key():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    cache["a"] = 2
    assert cache["a"] == 2
    assert len(cache) == 1


def test_delete_key():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    del cache["a"]
    assert len(cache) == 0
    assert "a" not in cache


def test_contains():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    assert "a" in cache
    assert "b" not in cache


def test_get_missing_raises_keyerror():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    with pytest.raises(KeyError):
        cache["missing"]


def test_iter():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    cache["b"] = 2
    assert set(iter(cache)) == {"a", "b"}


# ---- 频次记录 ----


def test_freq_increases_on_get():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    assert cache._freq["a"] == 1
    cache["a"]
    assert cache._freq["a"] == 2
    cache["a"]
    assert cache._freq["a"] == 3


def test_freq_increases_on_set_existing():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    assert cache._freq["a"] == 1
    cache["a"] = 2
    assert cache._freq["a"] == 2


# ---- 淘汰逻辑 ----


def test_eviction_removes_lowest_heat():
    """缓存满时淘汰 Heat 最小的 key。"""
    cache = TimeDecayedLFUCache(maxsize=3, halflife_seconds=60)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3

    # 多次访问 a 和 b，使它们成为热门
    cache["a"]
    cache["a"]
    cache["b"]

    # 插入 d，应淘汰 c（freq=1，最冷门）
    cache["d"] = 4
    assert "c" not in cache
    assert "a" in cache
    assert "b" in cache
    assert "d" in cache


def test_hot_key_survives_eviction():
    """高频访问的 key 不被淘汰。"""
    cache = TimeDecayedLFUCache(maxsize=2, halflife_seconds=60)
    cache["hot"] = 1
    cache["hot"]
    cache["hot"]
    cache["hot"]  # freq=4

    cache["cold"] = 2  # freq=1

    # 插入 new，cold (freq=1) 应被淘汰，hot (freq=4) 保留
    cache["new"] = 3
    assert "hot" in cache
    assert "cold" not in cache


def test_cold_key_evicted_first():
    """低频访问的 key 优先被淘汰。"""
    cache = TimeDecayedLFUCache(maxsize=2, halflife_seconds=60)
    cache["a"] = 1
    cache["b"] = 2

    # a 访问多次，b 只访问一次
    cache["a"]
    cache["a"]

    # 插入 c，b (freq=1) 应被淘汰
    cache["c"] = 3
    assert "a" in cache
    assert "b" not in cache


def test_maxsize_one():
    """maxsize=1 时每次插入都淘汰前一个。"""
    cache = TimeDecayedLFUCache(maxsize=1, halflife_seconds=60)
    cache["a"] = 1
    assert len(cache) == 1
    cache["b"] = 2
    assert len(cache) == 1
    assert "b" in cache
    assert "a" not in cache


def test_popitem_returns_key_value():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    key, value = cache.popitem()
    assert key == "a"
    assert value == 1
    assert len(cache) == 0


def test_popitem_empty_raises():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    with pytest.raises(KeyError):
        cache.popitem()


# ---- 时间衰减 ----


def test_heat_decreases_with_age():
    """Age 越大 Heat 越小（时间衰减）。"""
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=1.0)
    cache["a"] = 1

    heat_fresh = cache._heat("a", time.monotonic())
    time.sleep(0.05)
    heat_aged = cache._heat("a", time.monotonic())

    assert heat_aged < heat_fresh


def test_heat_increases_with_freq():
    """Freq 越大 Heat 越大。"""
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    cache["a"] = 1
    now = time.monotonic()

    heat_low = cache._heat("a", now)
    cache._freq["a"] = 10
    heat_high = cache._heat("a", now)

    assert heat_high > heat_low


def test_heat_halflife_decay():
    """经过一个半衰期后，Heat 减半。"""
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=10.0)
    cache["a"] = 1
    now = time.monotonic()

    heat_initial = cache._heat("a", now)
    heat_after_halflife = cache._heat("a", now + 10.0)  # Age = HalfLife

    assert heat_after_halflife == pytest.approx(heat_initial / 2, rel=1e-6)


def test_halflife_must_be_positive():
    with pytest.raises(ValueError, match="halflife_seconds 必须为正数"):
        TimeDecayedLFUCache(maxsize=10, halflife_seconds=0)
    with pytest.raises(ValueError, match="halflife_seconds 必须为正数"):
        TimeDecayedLFUCache(maxsize=10, halflife_seconds=-1)


# ---- LRU 对比：冷门搜索不挤掉热门照片 ----


def test_cold_search_does_not_evict_hot_photo():
    """LRU 的致命缺点：冷门搜索会挤掉热门照片。LFU 不会。"""
    cache = TimeDecayedLFUCache(maxsize=3, halflife_seconds=3600)

    # 热门照片被多次访问
    cache["hot_photo"] = "data"
    cache["hot_photo"]
    cache["hot_photo"]
    cache["hot_photo"]  # freq=4

    cache["warm_photo"] = "data"
    cache["warm_photo"]  # freq=2

    # 冷门搜索
    cache["cold_search"] = "data"  # freq=1
    cache["another_cold"] = "data"  # freq=1，触发淘汰

    # 热门照片应保留
    assert "hot_photo" in cache
    assert "warm_photo" in cache
    # 冷门搜索之一被淘汰
    assert "cold_search" not in cache or "another_cold" not in cache


# ---- currsize ----


def test_currsize():
    cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60)
    assert cache.currsize == 0
    cache["a"] = 1
    assert cache.currsize == 1
    cache["b"] = 2
    assert cache.currsize == 2
    del cache["a"]
    assert cache.currsize == 1

# =====================================================================
# 可插拔缓存后端测试（MemoryCache / RedisCache / 工厂降级）
# =====================================================================


def _cache_settings(**kwargs) -> Settings:
    """构造测试用 Settings，默认关闭 Redis。"""
    return Settings(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        **kwargs,
    )


def _make_fake_redis():
    """新建一个 fakeredis 异步实例（decode_responses=True）。"""
    import fakeredis.aioredis as fakeaioredis

    return fakeaioredis.FakeRedis(decode_responses=True)


# ---- MemoryCache：读写 / 过期 / 删除 / LRU 淘汰 ----


async def test_memory_cache_set_get():
    cache = MemoryCache(maxsize=10, default_ttl=60)
    await cache.set("k1", {"v": 1})
    assert await cache.get("k1") == {"v": 1}


async def test_memory_cache_get_missing_returns_none():
    cache = MemoryCache(maxsize=10, default_ttl=60)
    assert await cache.get("nope") is None


async def test_memory_cache_delete_existing():
    cache = MemoryCache(maxsize=10, default_ttl=60)
    await cache.set("k1", 1)
    assert await cache.delete("k1") is True
    assert await cache.get("k1") is None


async def test_memory_cache_delete_missing_returns_false():
    cache = MemoryCache(maxsize=10, default_ttl=60)
    assert await cache.delete("nope") is False


async def test_memory_cache_ttl_expire():
    cache = MemoryCache(maxsize=10, default_ttl=60)
    await cache.set("k1", "v1", ttl=0.05)
    assert await cache.get("k1") == "v1"
    # 留足余量：0.1s 在 Windows 定时器粒度与整套件负载下偶发不足（实测 p50≈0.109s）
    await asyncio.sleep(0.5)
    assert await cache.get("k1") is None


async def test_memory_cache_default_ttl_expire():
    cache = MemoryCache(maxsize=10, default_ttl=0.05)
    await cache.set("k1", "v1")
    assert await cache.get("k1") == "v1"
    # 同上：加宽时序余量，避免调度抖动导致的偶发失败
    await asyncio.sleep(0.5)
    assert await cache.get("k1") is None


async def test_memory_cache_lru_eviction():
    """maxsize 满时淘汰最久未访问的 key。"""
    cache = MemoryCache(maxsize=2, default_ttl=60)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.get("a")  # a 变为最近访问，b 变为最久未访问
    await cache.set("c", 3)  # 应淘汰 b
    assert await cache.get("a") == 1
    assert await cache.get("b") is None
    assert await cache.get("c") == 3


async def test_memory_cache_overwrite():
    cache = MemoryCache(maxsize=10, default_ttl=60)
    await cache.set("k1", 1)
    await cache.set("k1", 2)
    assert await cache.get("k1") == 2


# ---- RedisCache：读写 / 过期 / 删除 / key 前缀（fakeredis）----


async def test_redis_cache_set_get():
    r = _make_fake_redis()
    cache = RedisCache(redis=r, key_prefix="test:", default_ttl=60)
    await cache.set("k1", {"v": 1})
    assert await cache.get("k1") == {"v": 1}
    await r.aclose()


async def test_redis_cache_get_missing_returns_none():
    r = _make_fake_redis()
    cache = RedisCache(redis=r, key_prefix="test:", default_ttl=60)
    assert await cache.get("nope") is None
    await r.aclose()


async def test_redis_cache_delete_existing():
    r = _make_fake_redis()
    cache = RedisCache(redis=r, key_prefix="test:", default_ttl=60)
    await cache.set("k1", 1)
    assert await cache.delete("k1") is True
    assert await cache.get("k1") is None
    await r.aclose()


async def test_redis_cache_delete_missing_returns_false():
    r = _make_fake_redis()
    cache = RedisCache(redis=r, key_prefix="test:", default_ttl=60)
    assert await cache.delete("nope") is False
    await r.aclose()


async def test_redis_cache_ttl_expire():
    r = _make_fake_redis()
    cache = RedisCache(redis=r, key_prefix="test:", default_ttl=60)
    await cache.set("k1", "v1", ttl=0.05)
    assert await cache.get("k1") == "v1"
    await asyncio.sleep(0.1)
    assert await cache.get("k1") is None
    await r.aclose()


async def test_redis_cache_key_prefix_applied():
    """底层 Redis 中 key 应带前缀，值为 JSON 字符串。"""
    r = _make_fake_redis()
    cache = RedisCache(redis=r, key_prefix="imgsearch:", default_ttl=60)
    await cache.set("k1", "v1")
    raw = await r.get("imgsearch:k1")
    assert raw is not None
    assert json.loads(raw) == "v1"
    await r.aclose()


async def test_redis_cache_complex_value_serialization():
    r = _make_fake_redis()
    cache = RedisCache(redis=r, key_prefix="test:", default_ttl=60)
    payload = {"name": "照片", "tags": ["日落", "海边"], "score": 0.95}
    await cache.set("k1", payload)
    assert await cache.get("k1") == payload
    await r.aclose()


# ---- 工厂 get_cache：开关 / 降级 / 正常选择 ----


async def test_get_cache_disabled_uses_memory():
    """REDIS_ENABLED=false → MemoryCache。"""
    settings = _cache_settings(redis_enabled=False)
    redis_client = RedisClient(settings)
    await redis_client.connect()
    cache = await get_cache(settings, redis_client)
    assert isinstance(cache, MemoryCache)
    await redis_client.aclose()


async def test_get_cache_redis_unavailable_degrades_to_memory():
    """REDIS_ENABLED=true 但 Redis 连不上 → 降级 MemoryCache，不抛异常。"""
    settings = _cache_settings(
        redis_enabled=True,
        redis_url="redis://localhost:1/0",
        redis_timeout_seconds=0.5,
    )
    redis_client = RedisClient(settings)
    await redis_client.connect()  # 连接失败，不抛异常
    assert redis_client.connected is False
    cache = await get_cache(settings, redis_client)
    assert isinstance(cache, MemoryCache)
    await redis_client.aclose()


async def test_get_cache_redis_available_uses_redis():
    """REDIS_ENABLED=true 且健康 → RedisCache。"""
    settings = _cache_settings(redis_enabled=True, redis_key_prefix="imgsearch:")
    redis_client = RedisClient(settings)
    redis_client._redis = _make_fake_redis()
    redis_client._connected = True
    cache = await get_cache(settings, redis_client)
    assert isinstance(cache, RedisCache)
    await redis_client.aclose()


async def test_get_cache_redis_health_fail_degrades_to_memory():
    """REDIS_ENABLED=true 但 health_check 失败 → 降级 MemoryCache。"""
    settings = _cache_settings(redis_enabled=True)
    redis_client = RedisClient(settings)
    # 不连接，redis 为 None，health_check 返回 False
    cache = await get_cache(settings, redis_client)
    assert isinstance(cache, MemoryCache)
    await redis_client.aclose()


# ---- RedisClient 单元测试 ----


async def test_redis_client_disabled_skip_connect():
    """REDIS_ENABLED=false 时 connect 不连接，connected 为 False。"""
    settings = _cache_settings(redis_enabled=False)
    client = RedisClient(settings)
    await client.connect()
    assert client.connected is False
    assert client.redis is None
    await client.aclose()


async def test_redis_client_connect_failure_no_raise():
    """连接失败不抛异常，connected 为 False。"""
    settings = _cache_settings(
        redis_enabled=True,
        redis_url="redis://localhost:1/0",
        redis_timeout_seconds=0.5,
    )
    client = RedisClient(settings)
    await client.connect()
    assert client.connected is False
    await client.aclose()


async def test_redis_client_health_check_with_fake():
    """注入 fakeredis 后 health_check 返回 True。"""
    settings = _cache_settings(redis_enabled=True)
    client = RedisClient(settings)
    client._redis = _make_fake_redis()
    assert await client.health_check() is True
    assert client.connected is True
    await client.aclose()


async def test_redis_client_health_check_no_redis_returns_false():
    """未连接时 health_check 返回 False。"""
    settings = _cache_settings(redis_enabled=True)
    client = RedisClient(settings)
    assert await client.health_check() is False
    await client.aclose()
