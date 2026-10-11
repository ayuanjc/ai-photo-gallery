"""场景词典加载与白名单校验测试。"""
from __future__ import annotations

from app.scene.dictionary import (
    OTHER_SCENE,
    SceneDict,
    load_scene_dict,
)


def _dict() -> SceneDict:
    return load_scene_dict()


def test_load_scene_dict_has_ten_scenes():
    d = _dict()
    expected = {
        "nature",
        "city",
        "people",
        "life",
        "event",
        "animal",
        "architecture",
        "night",
        "vehicle",
        "art",
    }
    assert d.scene_keys == expected
    assert len(d.scene_keys) == 10
    assert d.is_valid_scene("nature")
    assert d.is_valid_scene("animal")
    assert not d.is_valid_scene("unknown")


def test_new_scenes_present():
    d = _dict()
    for key in ("animal", "architecture", "night", "vehicle", "art"):
        assert d.is_valid_scene(key)
        assert not d.is_valid_sub_scene(key)
    alias = d.scene_alias_map
    assert alias.get("动物") == "animal"
    assert alias.get("建筑") == "architecture"
    assert alias.get("夜景") == "night"
    assert alias.get("车辆") == "vehicle"
    assert alias.get("艺术") == "art"


def test_nature_children_expanded():
    d = _dict()
    for sub in ("ocean", "sky", "flower"):
        assert d.is_valid_sub_scene(sub)
        assert d.parent_of(sub) == "nature"


def test_people_children_expanded():
    d = _dict()
    for sub in ("portrait", "family"):
        assert d.is_valid_sub_scene(sub)
        assert d.parent_of(sub) == "people"


def test_weather_visual_attr():
    d = _dict()
    assert "weather" in d.visual_attrs
    assert set(d.visual_attrs["weather"]) == {"sunny", "cloudy", "rainy", "snow"}
    assert d.is_valid_visual_attr("weather", "sunny")
    assert not d.is_valid_visual_attr("weather", "storm")


def test_sub_scenes_within_whitelist():
    d = _dict()
    assert d.is_valid_sub_scene("beach")
    assert d.is_valid_sub_scene("dog")
    assert d.is_valid_sub_scene("night_view")
    assert not d.is_valid_sub_scene("submarine")


def test_parent_lookup():
    d = _dict()
    assert d.parent_of("beach") == "nature"
    assert d.parent_of("dog") == "animal"
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
    assert not d.is_valid_visual_attr("indoor", "balcony")


def test_to_dict_roundtrip():
    d = _dict()
    data = d.to_dict()
    assert set(data["scenes"]) == d.scene_keys
    assert set(data["visual_attrs"]) == {"time", "people", "indoor", "weather"}


def test_other_scene_constant():
    assert OTHER_SCENE == "other"