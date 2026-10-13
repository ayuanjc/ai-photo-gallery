"""搜索建议与纠错服务：基于场景词典 + 热门搜索词实现，不引外部模型。

数据来源：
    - 场景词典 label（中文）+ children（英文子场景）+ visual_attrs（视觉属性）
    - 热门搜索词（内存 LRU 计数，复用 TimeDecayedLFUCache）

匹配规则：
    - 建议接口：前缀匹配优先，其次包含匹配
    - 纠错接口：Levenshtein 编辑距离 <= 阈值时返回最近词典词
"""
from __future__ import annotations

import logging
import time

from ..core.config import Settings
from ..scene.dictionary import SceneDict, get_scene_dict
from ..utils.cache import TimeDecayedLFUCache

logger = logging.getLogger(__name__)

# 热门词热度半衰期：1 小时（越久没搜的词热度衰减越多）
_HOT_QUERY_HALFLIFE_SECONDS = 3600.0


class SuggestService:
    """搜索建议与纠错服务（纯内存，无外部依赖）。"""

    def __init__(self, settings: Settings, scene_dict: SceneDict) -> None:
        self._settings = settings
        self._scene_dict = scene_dict
        self._hot_queries: TimeDecayedLFUCache = TimeDecayedLFUCache(
            maxsize=max(1, settings.suggest_stats_cache_size),
            halflife_seconds=_HOT_QUERY_HALFLIFE_SECONDS,
        )
        self._vocabulary: list[str] = self._build_vocabulary()

    # ---- 词汇表构建 ----

    def _build_vocabulary(self) -> list[str]:
        """从场景词典构建词汇表：scene key + label + children + visual_attrs。"""
        words: list[str] = []
        for key, entry in self._scene_dict.scenes.items():
            words.append(key)
            label = entry.get("label")
            if isinstance(label, str) and label:
                words.append(label)
            for child in entry.get("children", []):
                if isinstance(child, str) and child:
                    words.append(child)
        for values in self._scene_dict.visual_attrs.values():
            for v in values:
                if isinstance(v, str) and v:
                    words.append(v)
        # 去重保序
        return list(dict.fromkeys(words))

    # ---- 热门词统计 ----

    def record_query(self, query: str) -> None:
        """记录一次搜索查询（用于热门词统计）。"""
        if not query or not self._settings.suggest_enabled:
            return
        try:
            self._hot_queries[query] = query
        except Exception as exc:
            logger.warning("热门词记录失败: %s", exc)

    def top_n(self, n: int) -> list[str]:
        """返回热度最高的 n 个查询词。"""
        if n <= 0:
            return []
        try:
            data = self._hot_queries._data
            if not data:
                return []
            now = time.monotonic()
            items = [(k, self._hot_queries._heat(k, now)) for k in data]
            items.sort(key=lambda x: x[1], reverse=True)
            return [k for k, _ in items[:n]]
        except Exception as exc:
            logger.warning("热门词 top_n 计算失败: %s", exc)
            return []

    # ---- 搜索建议 ----

    def suggest(self, q: str, limit: int) -> tuple[list[str], bool, list[str]]:
        """搜索建议：前缀匹配优先，其次包含匹配。

        返回 (suggestions, degraded, warnings)。
        """
        warnings: list[str] = []
        if not self._settings.suggest_enabled:
            return [], False, warnings

        q = (q or "").strip()
        if not q:
            return self.top_n(limit), False, warnings

        # 合并词汇表和热门词（热门词可能不在词汇表中）
        candidates = list(self._vocabulary)
        for hot in self._hot_queries._data:
            if hot not in candidates:
                candidates.append(hot)

        # 前缀匹配优先
        prefix_matches = [w for w in candidates if w.startswith(q) and w != q]
        # 包含匹配其次（排除已在前缀匹配中的）
        prefix_set = set(prefix_matches)
        contains_matches = [
            w for w in candidates
            if q in w and w != q and w not in prefix_set
        ]

        results = (prefix_matches + contains_matches)[:limit]
        return results, False, warnings

    # ---- 搜索纠错 ----

    def correct(self, q: str) -> tuple[str | None, bool, list[str]]:
        """搜索纠错：用 Levenshtein 编辑距离匹配场景词典。

        返回 (corrected, degraded, warnings)。
        """
        warnings: list[str] = []
        if not self._settings.suggest_enabled:
            return None, False, warnings

        q = (q or "").strip()
        if not q:
            return None, False, warnings

        max_dist = self._settings.suggest_edit_distance_max
        best_word: str | None = None
        best_dist = max_dist + 1

        for word in self._vocabulary:
            dist = self._levenshtein(q, word)
            if dist == 0:
                # 完全匹配，无需纠正
                return None, False, warnings
            if dist < best_dist:
                best_dist = dist
                best_word = word

        if best_word is not None and best_dist <= max_dist:
            return best_word, False, warnings
        return None, False, warnings

    # ---- Levenshtein 编辑距离（自己实现，不引依赖）----

    @staticmethod
    def _levenshtein(s1: str, s2: str) -> int:
        """计算两个字符串的 Levenshtein 编辑距离。

        操作：插入、删除、替换，每步代价 1。
        """
        if len(s1) < len(s2):
            return SuggestService._levenshtein(s2, s1)
        if len(s2) == 0:
            return len(s1)

        prev = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            curr = [i + 1]
            for j, c2 in enumerate(s2):
                insert = prev[j + 1] + 1
                delete = curr[j] + 1
                substitute = prev[j] + (c1 != c2)
                curr.append(min(insert, delete, substitute))
            prev = curr
        return prev[-1]


# ---- 模块级单例（供路由层获取，避免每次请求重建）----

_instance: SuggestService | None = None


def get_suggest_service(settings: Settings) -> SuggestService:
    """获取或创建 SuggestService 单例。"""
    global _instance
    if _instance is None:
        _instance = SuggestService(settings, get_scene_dict())
    return _instance


def reset_suggest_service() -> None:
    """重置单例（测试用）。"""
    global _instance
    _instance = None