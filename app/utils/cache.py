"""基于时间衰减的 LFU 缓存热度模型。

Heat = Freq * (2 ^ (-Age / HalfLife))

- Freq: key 的访问频次（每次 get/set 递增）
- Age: 距离最后一次访问的秒数
- HalfLife: 半衰期（秒），控制热度衰减速度

淘汰策略：当缓存满时，计算所有 key 的 Heat 值，淘汰 Heat 最小的 key。
热门 key（高频访问 + 近期访问）不易被淘汰，冷门 key（低频 + 长期未访问）优先淘汰。
"""
from __future__ import annotations

import time

from cachetools import Cache


class TimeDecayedLFUCache(Cache):
    """基于时间衰减的 LFU 缓存。

    继承 cachetools.Cache，重写 __getitem__/__setitem__/__delitem__ 等方法，
    自行维护 _data / _freq / _last_access 三个字典，不依赖 Cache 内部存储。

    参数:
        maxsize: 缓存最大容量。
        halflife_seconds: 热度半衰期（秒），越小衰减越快。
    """

    def __init__(self, maxsize: int, halflife_seconds: float, getsizeof=None) -> None:
        super().__init__(maxsize, getsizeof)
        if halflife_seconds <= 0:
            raise ValueError("halflife_seconds 必须为正数")
        self._halflife = halflife_seconds
        self._data: dict = {}
        self._freq: dict = {}
        self._last_access: dict = {}

    def __getitem__(self, key):
        value = self._data[key]
        self._freq[key] += 1
        self._last_access[key] = time.monotonic()
        return value

    def __setitem__(self, key, value):
        now = time.monotonic()
        if key in self._data:
            self._data[key] = value
            self._freq[key] += 1
            self._last_access[key] = now
            return
        while len(self._data) >= self.maxsize and self._data:
            self.popitem()
        self._data[key] = value
        self._freq[key] = 1
        self._last_access[key] = now

    def __delitem__(self, key):
        del self._data[key]
        del self._freq[key]
        del self._last_access[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key) -> bool:
        return key in self._data

    @property
    def currsize(self) -> int:
        return len(self._data)

    def popitem(self):
        """淘汰 Heat 值最小的 key，返回 (key, value)。"""
        if not self._data:
            raise KeyError("%s is empty" % self.__class__.__name__)
        now = time.monotonic()
        min_key = min(self._data, key=lambda k: self._heat(k, now))
        value = self._data[min_key]
        del self._data[min_key]
        del self._freq[min_key]
        del self._last_access[min_key]
        return min_key, value

    def _heat(self, key, now: float) -> float:
        """计算 key 的热度值：Heat = Freq * (2 ^ (-Age / HalfLife))。"""
        freq = self._freq.get(key, 1)
        age = now - self._last_access.get(key, now)
        return freq * (2 ** (-age / self._halflife))