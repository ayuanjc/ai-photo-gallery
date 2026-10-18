"""场景兜底模块测试：查询推断、默认场景、类型契约。"""
from __future__ import annotations

from types import SimpleNamespace

from app.scene.dictionary import get_scene_dict
from app.scene.fallback import (
    apply_fallback_scene,
    has_scene_content,
    infer_scene_from_query,
)
from app.scene.models import SceneTags


def _settings(fallback: str = "life") -> SimpleNamespace:
    return SimpleNamespace(plugin_fallback_scene=fallback)


class TestInferSceneFromQuery:
    def test_beach_query(self):
        inferred = infer_scene_from_query("海边度假", get_scene_dict())
        assert inferred is not None
        assert inferred.primary_scene == "nature"
        assert "nature" in inferred.scenes
        assert "beach" in inferred.sub_scenes

    def test_night_before_city(self):
        inferred = infer_scene_from_query("城市夜景", get_scene_dict())
        assert inferred is not None
        assert inferred.primary_scene == "night"
        assert "city" in inferred.scenes

    def test_multi_keyword(self):
        inferred = infer_scene_from_query("海边的猫咪", get_scene_dict())
        assert inferred is not None
        assert "nature" in inferred.scenes
        assert "animal" in inferred.scenes
        assert "beach" in inferred.sub_scenes
        assert "cat" in inferred.sub_scenes

    def test_whitespace_tolerant(self):
        assert infer_scene_from_query("  海边  ", get_scene_dict()) is not None

    def test_dictionary_label_match(self):
        inferred = infer_scene_from_query("自然风光", get_scene_dict())
        assert inferred is not None
        assert "nature" in inferred.scenes

    def test_unmatchable_returns_none(self):
        assert infer_scene_from_query("zzz", get_scene_dict()) is None

    def test_empty_query_returns_none(self):
        assert infer_scene_from_query("", get_scene_dict()) is None
        assert infer_scene_from_query("   ", get_scene_dict()) is None


class TestApplyFallbackSceneQueryInference:
    def test_query_inference_for_scenetags(self):
        tags = SceneTags(
            primary_scene=None, scenes=[], sub_scenes=[], visual_attrs={}
        )
        result = apply_fallback_scene(
            tags, _settings(), query="海边的日落", dictionary=get_scene_dict()
        )
        assert isinstance(result, SceneTags)
        assert result.primary_scene == "nature"
        assert result.confidence["nature"] == 0.4

    def test_query_inference_for_dict(self):
        result = apply_fallback_scene(
            None, _settings(), query="夜景", dictionary=get_scene_dict()
        )
        assert isinstance(result, dict)
        assert result["primary_scene"] == "night"
        assert result["confidence"]["night"] == 0.4

    def test_unmatchable_query_falls_to_default(self):
        result = apply_fallback_scene(
            None, _settings(), query="zzz", dictionary=get_scene_dict()
        )
        assert result["primary_scene"] == "life"
        assert result["confidence"]["life"] == 0.1

    def test_real_content_not_overridden(self):
        existing = {"primary_scene": "city", "scenes": ["city"]}
        result = apply_fallback_scene(
            existing, _settings(), query="海边", dictionary=get_scene_dict()
        )
        assert result is existing


class TestApplyFallbackSceneTypes:
    def test_none_returns_dict(self):
        result = apply_fallback_scene(None, _settings(), dictionary=get_scene_dict())
        assert isinstance(result, dict)
        assert result["primary_scene"] == "life"

    def test_empty_dict_returns_dict(self):
        result = apply_fallback_scene(
            {"primary_scene": None, "scenes": [], "sub_scenes": [], "visual_attrs": {}},
            _settings(),
            dictionary=get_scene_dict(),
        )
        assert isinstance(result, dict)
        assert result["primary_scene"] == "life"

    def test_empty_scenetags_returns_scenetags(self):
        tags = SceneTags(
            primary_scene=None, scenes=[], sub_scenes=[], visual_attrs={}
        )
        result = apply_fallback_scene(tags, _settings(), dictionary=get_scene_dict())
        assert isinstance(result, SceneTags)
        assert result.primary_scene == "life"

    def test_none_returns_none_when_disabled(self):
        assert apply_fallback_scene(None, _settings(""), dictionary=get_scene_dict()) is None

    def test_invalid_fallback_skipped(self):
        result = apply_fallback_scene(
            None, _settings("not_a_scene"), dictionary=get_scene_dict()
        )
        assert result is None


class TestHasSceneContentSceneTags:
    def test_none(self):
        assert has_scene_content(None) is False

    def test_empty_scenetags(self):
        tags = SceneTags(
            primary_scene=None, scenes=[], sub_scenes=[], visual_attrs={}
        )
        assert has_scene_content(tags) is False

    def test_scenetags_with_scenes(self):
        tags = SceneTags(primary_scene="life", scenes=["life"])
        assert has_scene_content(tags) is True

    def test_scenetags_with_visual_attrs_only(self):
        tags = SceneTags(visual_attrs={"time": "day"})
        assert has_scene_content(tags) is True
