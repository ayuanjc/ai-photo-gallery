"""基于时间衰减的 LFU 缓存热度模型测试。

Heat = Freq * (2 ^ (-Age / HalfLife))
"""
from __future__ import annotations

import time

import pytest

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