"""搜索建议与纠错测试：SuggestService 单元测试 + API 集成测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.scene.dictionary import get_scene_dict
from app.services.suggest_service import SuggestService, reset_suggest_service


# ============================================================================
# 辅助
# ============================================================================

def _settings(**overrides) -> Settings:
    defaults = dict(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        immich_timeout_seconds=1.0,
        immich_connect_timeout_seconds=1.0,
        immich_max_retries=1,
        immich_retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _service(**overrides) -> SuggestService:
    return SuggestService(_settings(**overrides), get_scene_dict())


# ============================================================================
# 单元测试：搜索建议
# ============================================================================

class TestSuggest:
    """suggest 方法测试：前缀匹配 / 包含匹配 / 空 q / 开关。"""

    def test_prefix_match_returns_label(self):
        """前缀匹配：输入"自"返回含"自然"的词。"""
        svc = _service()
        suggestions, _, _ = svc.suggest("自", 10)
        assert "自然" in suggestions

    def test_prefix_match_returns_scene_key(self):
        """前缀匹配：输入"nat"返回含"nature"的词。"""
        svc = _service()
        suggestions, _, _ = svc.suggest("nat", 10)
        assert "nature" in suggestions

    def test_contains_match_returns_child(self):
        """包含匹配：输入"art"返回含"party"的词（party 包含 art）。"""
        svc = _service()
        suggestions, _, _ = svc.suggest("art", 20)
        assert "party" in suggestions

    def test_empty_q_returns_hot_queries(self):
        """空 q：返回热门词。"""
        svc = _service()
        svc.record_query("海滩")
        svc.record_query("海滩")
        svc.record_query("日落")
        suggestions, _, _ = svc.suggest("", 10)
        assert "海滩" in suggestions
        assert "日落" in suggestions
        # 海滩搜了两次，热度更高，排前面
        assert suggestions.index("海滩") < suggestions.index("日落")

    def test_empty_q_no_hot_queries_returns_empty(self):
        """空 q 且无热门词：返回空列表。"""
        svc = _service()
        suggestions, _, _ = svc.suggest("", 10)
        assert suggestions == []

    def test_limit_caps_results(self):
        """limit 限制返回数量。"""
        svc = _service()
        suggestions, _, _ = svc.suggest("a", 2)
        assert len(suggestions) <= 2

    def test_suggest_disabled_returns_empty(self):
        """SUGGEST_ENABLED=false：返回空列表。"""
        svc = _service(suggest_enabled=False)
        suggestions, degraded, _ = svc.suggest("自", 10)
        assert suggestions == []
        assert degraded is False

    def test_prefix_takes_priority_over_contains(self):
        """前缀匹配排在包含匹配前面。"""
        svc = _service()
        suggestions, _, _ = svc.suggest("na", 50)
        # nature 是前缀匹配，应排在前面
        assert "nature" in suggestions
        # night_view 包含 "na"? n-i-g-h-t-_-v-i-e-w → 不含 "na"
        # natural 不在词汇表
        # 确认前缀匹配的词排在包含匹配前面
        prefix_words = [w for w in suggestions if w.startswith("na")]
        contains_words = [w for w in suggestions if "na" in w and not w.startswith("na")]
        if prefix_words and contains_words:
            assert suggestions.index(prefix_words[0]) < suggestions.index(contains_words[0])

    def test_q_itself_excluded(self):
        """q 本身不出现在建议中。"""
        svc = _service()
        suggestions, _, _ = svc.suggest("nature", 50)
        assert "nature" not in suggestions

    def test_hot_query_appears_in_suggestions(self):
        """热门词也参与建议匹配。"""
        svc = _service()
        svc.record_query("海滩日落")
        suggestions, _, _ = svc.suggest("海滩", 50)
        assert "海滩日落" in suggestions


# ============================================================================
# 单元测试：搜索纠错
# ============================================================================

class TestCorrect:
    """correct 方法测试：编辑距离纠错 / 无匹配 / 开关。"""

    def test_typo_corrected(self):
        """纠错：输入"natrue"返回"nature"（距离 2）。"""
        svc = _service()
        corrected, _, applied, _ = svc.correct("natrue")
        assert corrected == "nature"
        assert applied is True

    def test_exact_match_returns_none(self):
        """完全匹配：返回 None（无需纠正）。"""
        svc = _service()
        corrected, _, applied, _ = svc.correct("nature")
        assert corrected is None
        assert applied is False

    def test_no_match_returns_none(self):
        """纠错无匹配：corrected 为 None。"""
        svc = _service()
        corrected, _, applied, _ = svc.correct("xyz123abc")
        assert corrected is None
        assert applied is False

    def test_distance_one_corrected(self):
        """距离 1 也能纠正：输入"natur"返回"nature"。"""
        svc = _service()
        corrected, _, applied, _ = svc.correct("natur")
        assert corrected == "nature"
        assert applied is True

    def test_correct_disabled_returns_none(self):
        """CORRECT_ENABLED=false：返回 None。"""
        svc = _service(correct_enabled=False)
        corrected, _, applied, _ = svc.correct("natrue")
        assert corrected is None
        assert applied is False

    def test_empty_q_returns_none(self):
        """空 q：返回 None。"""
        svc = _service()
        corrected, _, applied, _ = svc.correct("")
        assert corrected is None
        assert applied is False

    def test_chinese_typo_corrected(self):
        """中文纠错：输入"自然x"距离 1 → 返回"自然"。"""
        svc = _service()
        corrected, _, applied, _ = svc.correct("自然x")
        assert corrected == "自然"
        assert applied is True

    def test_pinyin_corrected(self):
        """拼音缩写纠错：输入"hai bian"返回"海边"。"""
        svc = _service()
        corrected, confidence, applied, _ = svc.correct("hai bian")
        assert corrected == "海边"
        assert applied is True
        assert confidence >= 0.8

    def test_english_corrected(self):
        """英文纠错：输入"beach"返回"海边"。"""
        svc = _service()
        corrected, confidence, applied, _ = svc.correct("beach")
        assert corrected == "海边"
        assert applied is True
        assert confidence >= 0.7

    def test_low_confidence_not_applied(self):
        """置信度低于阈值时不纠错：applied=False。"""
        svc = _service(correct_confidence_threshold=0.99)
        corrected, _, applied, _ = svc.correct("natrue")
        assert applied is False


# ============================================================================
# 单元测试：Levenshtein
# ============================================================================

class TestLevenshtein:
    """Levenshtein 编辑距离测试。"""

    def test_identical_strings(self):
        assert SuggestService._levenshtein("abc", "abc") == 0

    def test_empty_string(self):
        assert SuggestService._levenshtein("", "abc") == 3
        assert SuggestService._levenshtein("abc", "") == 3

    def test_one_substitution(self):
        assert SuggestService._levenshtein("cat", "bat") == 1

    def test_one_insertion(self):
        assert SuggestService._levenshtein("cat", "cats") == 1

    def test_one_deletion(self):
        assert SuggestService._levenshtein("cats", "cat") == 1

    def test_transposition(self):
        assert SuggestService._levenshtein("ab", "ba") == 2


# ============================================================================
# API 集成测试
# ============================================================================

class TestSuggestAPI:
    """通过 TestClient 验证接口行为。"""

    def test_no_api_key_returns_401(self):
        """无 API Key 返回 401。"""
        from app.api.deps import get_settings_dep

        test_settings = _settings(search_api_key="secret-key")
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        try:
            with TestClient(app) as client:
                resp = client.get("/api/v1/search/suggest?q=test")
                assert resp.status_code == 401
        finally:
            app.dependency_overrides = original_overrides

    def test_suggest_endpoint_returns_suggestions(self):
        """suggest 接口正常返回建议。"""
        from app.api.deps import get_settings_dep

        test_settings = _settings()
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        reset_suggest_service()
        try:
            with TestClient(app) as client:
                resp = client.get(
                    "/api/v1/search/suggest?q=自",
                    headers={"x-api-key": ""},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert "suggestions" in data
                assert "自然" in data["suggestions"]
        finally:
            app.dependency_overrides = original_overrides
            reset_suggest_service()

    def test_correct_endpoint_returns_corrected(self):
        """correct 接口正常返回纠正词。"""
        from app.api.deps import get_settings_dep

        test_settings = _settings()
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        reset_suggest_service()
        try:
            with TestClient(app) as client:
                resp = client.get(
                    "/api/v1/search/correct?q=natrue",
                    headers={"x-api-key": ""},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["corrected"] == "nature"
        finally:
            app.dependency_overrides = original_overrides
            reset_suggest_service()

    def test_suggest_disabled_returns_empty_via_api(self):
        """SUGGEST_ENABLED=false + CORRECT_ENABLED=false：接口返回空结构。"""
        from app.api.deps import get_settings_dep

        test_settings = _settings(suggest_enabled=False, correct_enabled=False)
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        reset_suggest_service()
        try:
            with TestClient(app) as client:
                resp_suggest = client.get(
                    "/api/v1/search/suggest?q=自",
                    headers={"x-api-key": ""},
                )
                assert resp_suggest.status_code == 200
                assert resp_suggest.json()["suggestions"] == []

                resp_correct = client.get(
                    "/api/v1/search/correct?q=natrue",
                    headers={"x-api-key": ""},
                )
                assert resp_correct.status_code == 200
                assert resp_correct.json()["corrected"] is None
        finally:
            app.dependency_overrides = original_overrides
            reset_suggest_service()