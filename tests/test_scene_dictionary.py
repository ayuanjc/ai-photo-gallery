"""场景词典加载与白名单校验测试。"""
from __future__ import annotations

from app.scene.dictionary import (

    SceneDict,
    load_scene_dict,
)


def _dict() -> SceneDict:
    return load_scene_dict()


def test_load_scene_dict_has_five_scenes():
    d = _dict()
    assert d.scene_keys == {"nature", "city", "people", "life", "event"}
    assert d.is_valid_scene("nature")
    assert d.is_valid_scene("city")
    assert not d.is_valid_scene("unknown")


def test_sub_scenes_within_whitelist():
    d = _dict()
    assert d.is_valid_sub_scene("beach")
    assert d.is_valid_sub_scene("night_view")
    assert not d.is_valid_sub_scene("submarine")


def test_parent_lookup():
    d = _dict()
    assert d.parent_of("beach") == "nature"
    assert d.parent_of("wedding") == "people"
    assert d.parent_of("not_exist") is None


def test_scene_alias_map_includes_chinese_labels():
    d = _dict()
    alias = d.scene_alias_map
    assert alias.get("nature") == "nature"
    assert alias.get("自然") == "nature"
    assert alias.get("人物") == "people"


def test_visual_attr_whitelist():
    d = _dict()
    assert d.is_valid_visual_attr("time", "night")
    assert d.is_valid_visual_attr("indoor", "outdoor")
    assert not d.is_valid_visual_attr("time", "dawn")
    assert not d.is_valid_visual_attr("weather", "sunny")


def test_to_dict_roundtrip():
    d = _dict()
    data = d.to_dict()
    assert set(data["scenes"]) == d.scene_keys
    assert set(data["visual_attrs"]) == {"time", "people", "indoor"}
