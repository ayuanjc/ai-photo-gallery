"""P23: 向量索引服务 — 基于余弦相似度的内存向量索引。

用于以图搜图和语义搜索的向量检索能力：
- add(asset_id, vector): 添加向量
- search(query_vector, top_k): 余弦相似度 Top-K 检索
- remove(asset_id): 删除向量
- batch_add(items): 批量添加
- stats(): 索引统计信息

设计要点：
- 纯 Python + math 实现，无 numpy 依赖
- 线程安全：asyncio.Lock 保护写操作
- 归一化缓存：添加时预归一化，搜索时直接点积
- 容量限制：max_size 超限时 LRU 淘汰
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SIZE = 10000


def _normalize(vec: list[float]) -> list[float]:
    """L2 归一化。"""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm < 1e-10:
        return vec
    return [v / norm for v in vec]


def _dot(a: list[float], b: list[float]) -> float:
    """点积。"""
    return sum(x * y for x, y in zip(a, b))


class VectorIndex:
    """内存向量索引（余弦相似度）。"""

    def __init__(self, max_size: int = _DEFAULT_MAX_SIZE) -> None:
        self._max_size = max_size
        self._vectors: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def add(self, asset_id: str, vector: list[float]) -> None:
        """添加或更新向量。"""
        normalized = _normalize(vector)
        async with self._lock:
            if asset_id in self._vectors:
                self._vectors.move_to_end(asset_id)
                self._vectors[asset_id] = normalized
            else:
                if len(self._vectors) >= self._max_size:
                    self._vectors.popitem(last=False)
                self._vectors[asset_id] = normalized

    async def batch_add(self, items: list[tuple[str, list[float]]]) -> int:
        """批量添加，返回成功数。"""
        count = 0
        async with self._lock:
            for asset_id, vector in items:
                normalized = _normalize(vector)
                if asset_id in self._vectors:
                    self._vectors.move_to_end(asset_id)
                elif len(self._vectors) >= self._max_size:
                    self._vectors.popitem(last=False)
                self._vectors[asset_id] = normalized
                count += 1
        return count

    async def remove(self, asset_id: str) -> bool:
        """删除向量，返回是否存在。"""
        async with self._lock:
            if asset_id in self._vectors:
                del self._vectors[asset_id]
                return True
            return False

    async def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        exclude: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """余弦相似度 Top-K 检索。

        返回: [(asset_id, similarity), ...] 按相似度降序。
        """
        query_norm = _normalize(query_vector)
        exclude = exclude or set()

        async with self._lock:
            candidates: list[tuple[str, float]] = []
            for asset_id, vec in self._vectors.items():
                if asset_id in exclude:
                    continue
                sim = _dot(query_norm, vec)
                candidates.append((asset_id, sim))

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:top_k]

    async def get(self, asset_id: str) -> list[float] | None:
        """获取指定向量。"""
        async with self._lock:
            return self._vectors.get(asset_id)

    async def stats(self) -> dict[str, Any]:
        """索引统计。"""
        async with self._lock:
            return {
                "count": len(self._vectors),
                "max_size": self._max_size,
                "utilization": len(self._vectors) / self._max_size if self._max_size > 0 else 0,
            }

    async def clear(self) -> int:
        """清空索引，返回清除数量。"""
        async with self._lock:
            count = len(self._vectors)
            self._vectors.clear()
            return count