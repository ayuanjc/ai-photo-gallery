"""二次排序算法测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.ranking import blend_scene_weights, clamp, rank_assets, resolve_captured_at


def _asset(asset_id: str, score: float, exif_dt: str | None = None, file_created: str | None = None) -> dict:
    return {
        "id": asset_id,
        "score": score,
        "fileCreatedAt": file_created,
        "exifInfo": {"dateTimeOriginal": exif_dt},
    }


def test_clamp():
    assert clamp(-0.5) == 0.0
    assert clamp(1.5) == 1.0
    assert clamp(0.4) == 0.4


def test_resolve_captured_at_priority():
    asset = _asset("a", 0.5, exif_dt="2023-06-15T10:00:00Z", file_created="2022-01-01T00:00:00Z")
    dt, source = resolve_captured_at(asset, "1970-01-01T00:00:00+00:00")
    assert source == "exif"
    assert dt.year == 2023


def test_resolve_captured_at_fallback_to_file_created():
    asset = _asset("a", 0.5, exif_dt=None, file_created="2022-01-01T00:00:00Z")
    dt, source = resolve_captured_at(asset, "1970-01-01T00:00:00+00:00")
    assert source == "file_created"
    assert dt.year == 2022


def test_resolve_captured_at_default():
    asset = _asset("a", 0.5, exif_dt=None, file_created=None)
    dt, source = resolve_captured_at(asset, "1970-01-01T00:00:00+00:00")
    assert source == "default"
    assert dt.year == 1970


def test_rank_weighted_fusion_formula():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("a", 1.0, exif_dt="2023-06-15T10:00:00Z"),
        _asset("b", 0.9, exif_dt="2023-06-15T10:00:00Z"),
    ]
    ranked = rank_assets(
        assets,
        time_weight=0.0,
        similarity_weight=1.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    # 权重全部给相似度时，应按相似度降序
    assert ranked[0][0]["id"] == "a"
    assert ranked[1][0]["id"] == "b"


def test_rank_time_weight_prefers_newer():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("old", 1.0, exif_dt="2000-01-01T00:00:00Z"),
        _asset("new", 0.0, exif_dt="2023-12-31T00:00:00Z"),
    ]
    ranked = rank_assets(
        assets,
        time_weight=1.0,
        similarity_weight=0.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert ranked[0][0]["id"] == "new"


def test_rank_sorted_descending():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("x", 0.5, exif_dt="2023-01-01T00:00:00Z"),
        _asset("y", 0.9, exif_dt="2023-06-01T00:00:00Z"),
        _asset("z", 0.1, exif_dt="2023-12-01T00:00:00Z"),
    ]
    ranked = rank_assets(
        assets,
        time_weight=0.3,
        similarity_weight=0.7,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    finals = [r[1] for r in ranked]
    assert finals == sorted(finals, reverse=True)

# ---- score 缺失时的排名反推（Immich smart search 无显式 score）----


def _asset_no_score(asset_id: str, exif_dt: str | None = None, file_created: str | None = None) -> dict:
    return {
        "id": asset_id,
        "fileCreatedAt": file_created,
        "exifInfo": {"dateTimeOriginal": exif_dt},
    }


def test_rank_missing_score_uses_rank_position():
    """score 缺失时按 Immich 返回顺序反推相似度：第 1 名 > 末位。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset_no_score("first", exif_dt="2023-06-15T10:00:00Z"),
        _asset_no_score("second", exif_dt="2023-06-15T10:00:00Z"),
        _asset_no_score("third", exif_dt="2023-06-15T10:00:00Z"),
    ]
    ranked = rank_assets(
        assets,
        time_weight=0.0,
        similarity_weight=1.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert ranked[0][0]["id"] == "first"
    assert ranked[1][0]["id"] == "second"
    assert ranked[2][0]["id"] == "third"
    # 纯相似度权重下 final 即 similarity：首名 1.0，末位 0.5
    assert ranked[0][2] == 1.0
    assert ranked[2][2] == 0.5


def test_rank_single_asset_missing_score():
    """单条结果无 score：rank 分母为 0 时应安全取 1.0。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ranked = rank_assets(
        [_asset_no_score("only", exif_dt="2023-06-15T10:00:00Z")],
        time_weight=0.0,
        similarity_weight=1.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert ranked[0][2] == 1.0


def test_rank_explicit_score_preferred_over_position():
    """显式 score 优先于排名反推（兼容未来 Immich 返回 score）。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("low", 0.2, exif_dt="2023-06-15T10:00:00Z"),
        _asset_no_score("positional", exif_dt="2023-06-15T10:00:00Z"),
    ]
    ranked = rank_assets(
        assets,
        time_weight=0.0,
        similarity_weight=1.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    # 排第 2 的无 score 图反推 0.5，高于排第 1 但显式 score=0.2 的图
    assert ranked[0][0]["id"] == "positional"
    assert ranked[0][2] == 0.5
    assert ranked[1][2] == 0.2

# ---- blend_scene_weights 单元测试 ----


def test_blend_single_scene_matches_resolve():
    """单场景一致：blend 对单场景应与 SCENE_WEIGHTS 表取值一致。"""
    assert blend_scene_weights(["people"], 0.3, 0.7) == (0.5, 0.5)
    assert blend_scene_weights(["nature"], 0.3, 0.7) == (0.2, 0.8)
    assert blend_scene_weights(["event"], 0.3, 0.7) == (0.4, 0.6)


def test_blend_multiple_scenes_arithmetic_average():
    """多场景平均：对多个合法场景取算术平均后归一化。"""
    alpha, beta = blend_scene_weights(["people", "nature"], 0.3, 0.7)
    assert alpha == 0.35  # (0.5 + 0.2) / 2
    assert beta == 0.65  # (0.5 + 0.8) / 2

    alpha, beta = blend_scene_weights(["people", "nature", "event"], 0.3, 0.7)
    assert alpha == (0.5 + 0.2 + 0.4) / 3
    assert beta == (0.5 + 0.8 + 0.6) / 3


def test_blend_empty_scenes_falls_back_to_default():
    """空场景回退 default：scenes 为空列表时回退到传入的默认权重。"""
    assert blend_scene_weights([], 0.3, 0.7) == (0.3, 0.7)


def test_blend_invalid_scenes_fall_back_to_default():
    """非法场景回退 default：全部场景不在 SCENE_WEIGHTS 中时回退默认。"""
    assert blend_scene_weights(["unknown"], 0.3, 0.7) == (0.3, 0.7)
    assert blend_scene_weights(["default"], 0.3, 0.7) == (0.3, 0.7)
    assert blend_scene_weights(["foo", "bar"], 0.3, 0.7) == (0.3, 0.7)


def test_blend_mixed_legal_and_illegal_only_uses_legal():
    """混合场景：合法 + 非法只按合法的算平均。"""
    # people + unknown → 只用 people
    assert blend_scene_weights(["people", "unknown"], 0.3, 0.7) == (0.5, 0.5)
    # people + nature + unknown + default → 只用 people + nature
    alpha, beta = blend_scene_weights(["people", "nature", "unknown", "default"], 0.3, 0.7)
    assert alpha == 0.35
    assert beta == 0.65


# ---- rank_assets 多场景集成测试 ----


def test_rank_assets_primary_scene_only_behavior_unchanged():
    """集成：只有 primary_scene（无 scenes）时行为与单场景一致。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    a1 = _asset("a1", 0.8, exif_dt="2023-06-15T10:00:00Z")
    a1["primary_scene"] = "people"
    a2 = _asset("a2", 0.8, exif_dt="2023-06-15T10:00:00Z")
    a2["scenes"] = ["people"]
    ranked = rank_assets(
        [a1, a2],
        time_weight=0.3,
        similarity_weight=0.7,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    # 两者权重相同 → final_score 相同
    assert ranked[0][1] == ranked[1][1]


def test_rank_assets_multiple_scenes_blends_weights():
    """集成：有多个 scenes 时按综合平均权重排序。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # scenes=["people", "nature"] → (0.35, 0.65)，更偏相似度
    # scenes=["people"] → (0.5, 0.5)
    # 同样 score=1.0、同样较旧日期，偏相似度的 blend 资产 final 更高 → 排第一。
    a_blend = _asset("blend", 1.0, exif_dt="2023-01-01T00:00:00Z")
    a_blend["scenes"] = ["people", "nature"]
    a_single = _asset("single", 1.0, exif_dt="2023-01-01T00:00:00Z")
    a_single["scenes"] = ["people"]
    ranked = rank_assets(
        [a_blend, a_single],
        time_weight=0.3,
        similarity_weight=0.7,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert ranked[0][0]["id"] == "blend"


def test_rank_assets_no_scenes_no_primary_falls_back_to_default():
    """集成：scenes 空且无 primary_scene 时回退到传入的默认权重。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    a = _asset("a", 0.8, exif_dt="2023-06-15T10:00:00Z")
    ranked = rank_assets(
        [a],
        time_weight=0.0,
        similarity_weight=1.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    # default 权重 (0.0, 1.0) → final = 1.0 * similarity = 0.8
    assert ranked[0][1] == 0.8
