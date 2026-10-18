"""搜索建议与纠错服务：基于场景词典 + 热门搜索词实现，不引外部模型。

数据来源：
    - 场景词典 label（中文）+ children（英文子场景）+ visual_attrs（视觉属性）
    - 热门搜索词（内存 LRU 计数，复用 TimeDecayedLFUCache）

匹配规则：
    - 建议接口：bisect 二分查找前缀匹配（O(log n + k)），其次包含匹配
    - 纠错接口：拼音缩写映射 + 英文大小写归一化 + Levenshtein 编辑距离 + 置信度阈值
"""
from __future__ import annotations

import bisect
import logging
import time

from ..core.config import Settings
from ..scene.dictionary import SceneDict, get_scene_dict
from ..utils.cache import TimeDecayedLFUCache

logger = logging.getLogger(__name__)

_HOT_QUERY_HALFLIFE_SECONDS = 3600.0

_PINYIN_MAP: dict[str, str] = {
    "hai bian": "海边", "haibian": "海边",
    "ri luo": "日落", "riluo": "日落",
    "shan feng": "山峰", "shanfeng": "山峰",
    "xue jing": "雪景", "xuejing": "雪景",
    "ye jing": "夜景", "yejing": "夜景",
    "hua hui": "花卉", "huahui": "花卉",
    "jian zhu": "建筑", "jianzhu": "建筑",
    "mei shi": "美食", "meishi": "美食",
    "chong wu": "宠物", "chongwu": "宠物",
    "er tong": "儿童", "ertong": "儿童",
    "yun dong": "运动", "yundong": "运动",
}

_EN_CN_MAP: dict[str, str] = {
    "beach": "海边", "sunset": "日落", "mountain": "山峰",
    "snow": "雪景", "night": "夜景", "flower": "花卉",
    "building": "建筑", "food": "美食", "pet": "宠物",
    "child": "儿童", "kid": "儿童", "sport": "运动",
    "ocean": "海边", "sea": "海边", "dawn": "日出",
    "sunrise": "日出", "forest": "森林", "river": "河流",
}


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
        self._sorted_vocab: list[str] = sorted(self._vocabulary)

    def _build_vocabulary(self) -> list[str]:
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
        return list(dict.fromkeys(words))

    def record_query(self, query: str) -> None:
        if not query or not self._settings.suggest_enabled:
            return
        try:
            self._hot_queries[query] = query
        except Exception as exc:
            logger.warning("热门词记录失败: %s", exc)

    def top_n(self, n: int) -> list[str]:
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

    def _bisect_prefix(self, q: str, candidates: list[str], limit: int) -> list[str]:
        """用 bisect 在已排序列表上做前缀匹配，O(log n + k)。"""
        left = bisect.bisect_left(candidates, q)
        results: list[str] = []
        i = left
        while i < len(candidates) and len(results) < limit:
            w = candidates[i]
            if not w.startswith(q):
                break
            if w != q:
                results.append(w)
            i += 1
        return results

    def suggest(self, q: str, limit: int) -> tuple[list[str], bool, list[str]]:
        """搜索建议：bisect 前缀匹配优先，其次包含匹配。

        返回 (suggestions, degraded, warnings)。
        """
        warnings: list[str] = []
        if not self._settings.suggest_enabled:
            return [], False, warnings

        q = (q or "").strip()
        if not q:
            return self.top_n(limit), False, warnings

        sorted_candidates = self._sorted_vocab
        hot_words = sorted(
            w for w in self._hot_queries._data if w not in set(self._vocabulary)
        )
        if hot_words:
            merged: list[str] = []
            i = j = 0
            while i < len(sorted_candidates) and j < len(hot_words):
                if sorted_candidates[i] <= hot_words[j]:
                    merged.append(sorted_candidates[i])
                    i += 1
                else:
                    merged.append(hot_words[j])
                    j += 1
            merged.extend(sorted_candidates[i:])
            merged.extend(hot_words[j:])
            sorted_candidates = merged

        prefix_matches = self._bisect_prefix(q, sorted_candidates, limit)
        if len(prefix_matches) >= limit:
            return prefix_matches[:limit], False, warnings

        prefix_set = set(prefix_matches)
        all_candidates = self._vocabulary + [
            w for w in self._hot_queries._data if w not in set(self._vocabulary)
        ]
        contains_matches = [
            w for w in all_candidates
            if q in w and w != q and w not in prefix_set
        ]

        results = (prefix_matches + contains_matches)[:limit]
        return results, False, warnings

    def correct(self, q: str) -> tuple[str | None, float, bool, list[str]]:
        """搜索纠错：拼音映射 → 英文映射 → Levenshtein 编辑距离。

        返回 (corrected, confidence, applied, warnings)。
        置信度低于阈值时 applied=false，原样返回。
        """
        warnings: list[str] = []
        if not getattr(self._settings, "correct_enabled", True):
            return None, 0.0, False, warnings

        q = (q or "").strip()
        if not q:
            return None, 0.0, False, warnings

        q_lower = q.lower()

        if q_lower in _PINYIN_MAP:
            return _PINYIN_MAP[q_lower], 0.9, True, warnings

        if q_lower in _EN_CN_MAP:
            return _EN_CN_MAP[q_lower], 0.8, True, warnings

        for en, cn in _EN_CN_MAP.items():
            if q_lower == en or q_lower == en.capitalize():
                return cn, 0.8, True, warnings

        max_dist = self._settings.suggest_edit_distance_max
        best_word: str | None = None
        best_dist = max_dist + 1

        for word in self._vocabulary:
            dist = self._levenshtein(q_lower, word.lower())
            if dist == 0:
                return None, 1.0, False, warnings
            if dist < best_dist:
                best_dist = dist
                best_word = word

        if best_word is not None and best_dist <= max_dist:
            confidence = 1.0 - best_dist / max(len(q), len(best_word), 1)
            threshold = getattr(self._settings, "correct_confidence_threshold", 1.0)
            if confidence >= threshold:
                return best_word, round(confidence, 4), True, warnings
            return best_word, round(confidence, 4), False, warnings

        return None, 0.0, False, warnings

    @staticmethod
    def _levenshtein(s1: str, s2: str) -> int:
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


_instance: SuggestService | None = None


def get_suggest_service(settings: Settings) -> SuggestService:
    global _instance
    if _instance is None:
        _instance = SuggestService(settings, get_scene_dict())
    return _instance


def reset_suggest_service() -> None:
    global _instance
    _instance = None
