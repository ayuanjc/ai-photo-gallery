"""按主场景动态选择排序权重的测试。"""
from __future__ import annotations

from datetime import datetime, timezone


from app.services.ranking import SCENE_WEIGHTS, rank_assets


def test_scene_weights_table_present():
    assert set(SCENE_WEIGHTS) == {"people", "nature", "event"}
    for key in ("people", "nature", "event"):
        assert {"alpha", "beta"} <= set(SCENE_WEIGHTS[key])


def _asset(aid: str, score: float, primary_scene: str | None = None) -> dict:
    return {
        "id": aid,
        "score": score,
        "primary_scene": primary_scene,
        "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"},
    }


def test_rank_assets_uses_per_asset_scene_weights():
    """同一时间下，不同主场景应使用不同的融合权重。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # 两条 asset 时间、相似度相同，唯一差异是主场景。
    nature = _asset("nature", 1.0, primary_scene="nature")
    people = _asset("people", 1.0, primary_scene="people")
    ranked = rank_assets(
        [nature, people],
        time_weight=0.3,
        similarity_weight=0.7,  # 作为 default，这里两者都命中了场景，不会用到
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    finals = {r[0]["id"]: r[1] for r in ranked}
    # nature: alpha=0.2, beta=0.8; people: alpha=0.5, beta=0.5。
    # 时间新鲜度 fres 与相似度 sim 对两条 asset 一致，因此最终分仅由权重决定。
    assert finals["nature"] != finals["people"]


def test_rank_assets_falls_back_to_default_weight_when_no_scene():
    """未携带 primary_scene 时使用传入的默认权重，保持原有行为。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("a", 1.0, primary_scene=None),
        _asset("b", 0.9, primary_scene=None),
    ]
    ranked = rank_assets(
        assets,
        time_weight=0.0,
        similarity_weight=1.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert ranked[0][0]["id"] == "a"
    assert ranked[1][0]["id"] == "b"