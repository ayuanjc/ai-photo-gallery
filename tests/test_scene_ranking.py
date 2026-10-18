"""场景标签不得参与搜索排序的回归测试（R4）。

历史缺陷：SCENE_WEIGHTS 场景动态权重曾参与 blend 排序，导致
"场景输出 2~3 个大场景、confidence 全 0.5" 且顺序不可信。
该机制已在 T1 移除，本文件守卫它不被悄悄加回来。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import ranking


def test_scene_weights_removed():
    """SCENE_WEIGHTS 与 blend_scene_weights/rank_assets 必须不存在。"""
    assert not hasattr(ranking, "SCENE_WEIGHTS")
    assert not hasattr(ranking, "blend_scene_weights")
    assert not hasattr(ranking, "rank_assets")


def _asset(aid: str, score: float, primary_scene: str | None) -> dict:
    return {
        "id": aid,
        "score": score,
        "primary_scene": primary_scene,
        "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"},
    }


def test_scenes_do_not_affect_blend_final():
    """带场景字段与去掉场景字段，final 必须完全一致。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with_scenes = [
        _asset("nature", 1.0, "nature"),
        _asset("people", 1.0, "people"),
    ]
    stripped = [
        {"id": "nature", "score": 1.0, "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"}},
        {"id": "people", "score": 1.0, "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"}},
    ]
    kwargs = dict(
        mode="blend",
        time_weight=0.3,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    finals_with = [r["final"] for r in ranking.rank_results(with_scenes, **kwargs)]
    finals_stripped = [r["final"] for r in ranking.rank_results(stripped, **kwargs)]
    assert finals_with == finals_stripped


def test_scenes_do_not_affect_immich_order():
    """immich 模式下场景字段对顺序无影响。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("a", 1.0, "nature"),
        _asset("b", 0.9, None),
    ]
    ranked = ranking.rank_results(
        assets,
        mode="immich",
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert [r["asset"]["id"] for r in ranked] == ["a", "b"]
