"""搜索反馈闭环服务。

记录用户对搜索结果的点击/跳过行为，供排序时做微加权。
反馈数据匿名存储在 CacheBackend 中，key 按 asset_id + query 哈希，
不存储任何用户身份信息。
"""
from __future__ import annotations

import hashlib
import logging

from ..core.cache import CacheBackend
from ..core.config import Settings

logger = logging.getLogger(__name__)

VALID_ACTIONS = frozenset({"click", "skip"})


class FeedbackService:
    """搜索反馈服务。

    - record_feedback：记录用户行为（click/skip），累加计数。
    - get_feedback_boost：根据历史反馈计算加权值（-max_boost ~ +max_boost）。

    反馈数据结构（存入 CacheBackend）：
        {"click": int, "skip": int}

    加权公式：
        raw = (clicks - skips) / (clicks + skips)   # [-1, 1]
        boost = raw * feedback_boost_max            # [-max, +max]
    """

    def __init__(self, cache: CacheBackend, settings: Settings) -> None:
        self._cache = cache
        self._settings = settings

    async def record_feedback(self, asset_id: str, query: str, action: str) -> None:
        """记录一次用户反馈。

        action 为 "click" 或 "skip"，其他值静默忽略。
        feedback_enabled=False 时直接返回，不写缓存。

        注意：此方法采用 read-then-write 模式，在并发场景下可能丢失少量计数
        （非原子操作）。这是有意为之的权衡：反馈数据为统计近似值，少量丢失
        可接受，且整个操作已用 try/except 包裹，不会因并发导致崩溃。
        """
        if not self._settings.feedback_enabled:
            return
        if action not in VALID_ACTIONS:
            logger.warning("无效的反馈动作: %s，跳过记录", action)
            return
        key = self._make_key(asset_id, query)
        try:
            existing = await self._cache.get(key)
            if not isinstance(existing, dict):
                existing = {"click": 0, "skip": 0}
            existing[action] = existing.get(action, 0) + 1
            await self._cache.set(
                key, existing, ttl=self._settings.feedback_ttl_seconds
            )
        except Exception as exc:
            logger.warning(
                "反馈记录失败: asset_id=%s query=%s err=%s", asset_id, query, exc
            )

    async def get_feedback_boost(self, asset_id: str, query: str) -> float:
        """获取反馈加权值。

        返回 [-feedback_boost_max, +feedback_boost_max] 范围内的浮点数。
        feedback_enabled=False 或无反馈数据时返回 0.0。
        """
        if not self._settings.feedback_enabled:
            return 0.0
        key = self._make_key(asset_id, query)
        try:
            data = await self._cache.get(key)
        except Exception as exc:
            logger.warning(
                "反馈读取失败: asset_id=%s query=%s err=%s", asset_id, query, exc
            )
            return 0.0
        if not isinstance(data, dict):
            return 0.0
        clicks = int(data.get("click", 0))
        skips = int(data.get("skip", 0))
        total = clicks + skips
        if total == 0:
            return 0.0
        max_boost = self._settings.feedback_boost_max
        raw = (clicks - skips) / total
        boost = raw * max_boost
        return max(-max_boost, min(max_boost, boost))

    @staticmethod
    def _make_key(asset_id: str, query: str) -> str:
        """生成缓存 key：feedback:{asset_id}:{sha256(query)[:16]}。"""
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return f"feedback:{asset_id}:{query_hash}"


_feedback_service: FeedbackService | None = None


def init_feedback_service(cache: CacheBackend, settings: Settings) -> FeedbackService:
    """初始化进程级 FeedbackService 单例。"""
    global _feedback_service
    _feedback_service = FeedbackService(cache, settings)
    return _feedback_service


def get_feedback_service() -> FeedbackService | None:
    """获取已初始化的 FeedbackService 单例，未初始化返回 None。"""
    return _feedback_service


def reset_feedback_service() -> None:
    """重置单例（测试用）。"""
    global _feedback_service
    _feedback_service = None