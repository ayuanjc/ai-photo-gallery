"""搜索反馈闭环服务。

记录用户对搜索结果的点击/跳过行为，供排序时做微加权。
反馈数据匿名存储在 CacheBackend 中，key 按 asset_id + query 哈希，
不存储任何用户身份信息。

增强特性：
- 指数衰减：越久的反馈权重越低（半衰期可配置）
- 防刷：同一 session 对同一 asset 单位时间内反馈限次
- boost 叠加：apply_feedback_boost 将 boost 叠加到 score 并裁切到 [0,1]
"""
from __future__ import annotations

import hashlib
import logging
import math
import time

from ..core.cache import CacheBackend
from ..core.config import Settings

logger = logging.getLogger(__name__)

VALID_ACTIONS = frozenset({"click", "skip"})


class FeedbackService:
    """搜索反馈服务。

    - record_feedback：记录用户行为（click/skip），累加计数 + 防刷。
    - get_feedback_boost：根据历史反馈计算加权值（-max_boost ~ +max_boost），含指数衰减。
    - apply_feedback_boost：将 boost 叠加到 score 并裁切到 [0,1]。

    反馈数据结构（存入 CacheBackend）：
        {"click": int, "skip": int, "last_ts": float}

    加权公式（含指数衰减）：
        raw = (clicks - skips) / (clicks + skips)   # [-1, 1]
        decay = 2^(-age / halflife)                 # [0, 1]
        boost = raw * feedback_boost_max * decay    # [-max*decay, +max*decay]
    """

    def __init__(self, cache: CacheBackend, settings: Settings) -> None:
        self._cache = cache
        self._settings = settings
        self._rate_limiter: dict[str, list[float]] = {}

    async def record_feedback(
        self, asset_id: str, query: str, action: str, session_id: str = ""
    ) -> tuple[bool, str | None]:
        """记录一次用户反馈。

        返回 (recorded, reason)。
        action 为 "click" 或 "skip"，其他值静默忽略。
        feedback_enabled=False 时直接返回。
        防刷：同一 session + asset + query 每分钟超过 feedback_rate_limit_per_minute 次时拒绝。

        注意：read-then-write 非原子，并发可能丢失少量计数（有意权衡）。
        """
        if not self._settings.feedback_enabled:
            return True, None
        if action not in VALID_ACTIONS:
            logger.warning("无效的反馈动作: %s，跳过记录", action)
            return False, "invalid_action"

        if not self._check_rate_limit(asset_id, query, session_id):
            return False, "rate_limited"

        key = self._make_key(asset_id, query)
        try:
            existing = await self._cache.get(key)
            if not isinstance(existing, dict):
                existing = {"click": 0, "skip": 0, "last_ts": 0.0}
            existing[action] = existing.get(action, 0) + 1
            existing["last_ts"] = time.time()
            await self._cache.set(
                key, existing, ttl=self._settings.feedback_ttl_seconds
            )
        except Exception as exc:
            logger.warning(
                "反馈记录失败: asset_id=%s query=%s err=%s", asset_id, query, exc
            )
            return False, "cache_error"
        return True, None

    async def get_feedback_boost(self, asset_id: str, query: str) -> float:
        """获取反馈加权值（含指数衰减）。

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

        last_ts = float(data.get("last_ts", 0.0))
        if last_ts > 0:
            age = time.time() - last_ts
            halflife = getattr(self._settings, "feedback_decay_halflife_seconds", 86400.0)
            decay = math.pow(2, -age / max(halflife, 1.0))
        else:
            decay = 1.0

        boost = raw * max_boost * decay
        return max(-max_boost, min(max_boost, boost))

    def apply_feedback_boost(self, score: float, boost: float) -> float:
        """将 boost 叠加到 score 并裁切到 [0, 1]。"""
        return max(0.0, min(1.0, score + boost))

    def _check_rate_limit(self, asset_id: str, query: str, session_id: str) -> bool:
        """防刷检查：同一 session + asset + query 每分钟反馈限次。"""
        if not session_id:
            return True
        limit = getattr(self._settings, "feedback_rate_limit_per_minute", 2)
        if limit <= 0:
            return True

        rl_key = f"{session_id}:{asset_id}:{hashlib.sha256(query.encode()).hexdigest()[:8]}"
        now = time.monotonic()
        window = 60.0

        timestamps = self._rate_limiter.get(rl_key, [])
        timestamps = [t for t in timestamps if now - t < window]
        if len(timestamps) >= limit:
            return False
        timestamps.append(now)
        self._rate_limiter[rl_key] = timestamps
        return True

    @staticmethod
    def _make_key(asset_id: str, query: str) -> str:
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return f"feedback:{asset_id}:{query_hash}"


_feedback_service: FeedbackService | None = None


def init_feedback_service(cache: CacheBackend, settings: Settings) -> FeedbackService:
    global _feedback_service
    _feedback_service = FeedbackService(cache, settings)
    return _feedback_service


def get_feedback_service() -> FeedbackService | None:
    return _feedback_service


def reset_feedback_service() -> None:
    global _feedback_service
    _feedback_service = None