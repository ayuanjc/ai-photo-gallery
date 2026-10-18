"""排序测试：immich 顺序保持 / blend RRF 公式 / 相似度不编造。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.ranking import (
    clamp,
    rank_results,
    resolve_captured_at,
)


def _asset(asset_id: str, score: float | None, exif_dt: str | None = None, file_created: str | None = None) -> dict:
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


# ---- immich 模式（默认）：顺序保持 + 不编造分数 ----


def test_immich_mode_preserves_upstream_order():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("a", 0.9, exif_dt="2023-06-15T10:00:00Z"),
        _asset("b", 1.0, exif_dt="2023-06-15T10:00:00Z"),
    ]
    ranked = rank_results(
        assets,
        mode="immich",
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    # 顺序与 Immich 返回完全一致，即使 b 的 score 更高也不重排
    assert [r["asset"]["id"] for r in ranked] == ["a", "b"]
    assert [r["immich_rank"] for r in ranked] == [0, 1]
    assert all(r["final"] is None for r in ranked)


def test_immich_mode_similarity_only_when_upstream_score():
    """similarity 仅在上游返回真实 score 时有值（不再按名次反推）。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("has", 0.9, exif_dt="2023-06-15T10:00:00Z"),
        _asset("none", None, exif_dt="2023-06-15T10:00:00Z"),
    ]
    ranked = rank_results(
        assets,
        mode="immich",
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert ranked[0]["similarity"] == 0.9
    assert ranked[0]["similarity_source"] == "upstream"
    # 无 score 时 similarity=None（回归守卫：禁止名次反推伪造相似度）
    assert ranked[1]["similarity"] is None
    assert ranked[1]["similarity_source"] == "none"


# ---- blend 模式：RRF 先验 + 时间新鲜度，w_time 上限 0.2 ----


def test_blend_mode_formula():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("a", 1.0, exif_dt="2023-06-15T10:00:00Z"),
        _asset("b", 0.9, exif_dt="2023-06-15T10:00:00Z"),
    ]
    ranked = rank_results(
        assets,
        mode="blend",
        time_weight=0.3,  # 会被截断到 0.2
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    finals = [r["final"] for r in ranked]
    assert finals == sorted(finals, reverse=True)
    # prior(0) = 1.0 > prior(1) = 60/61，同时间 → a 在前
    assert ranked[0]["asset"]["id"] == "a"
    # w 被截断为 0.2：final = 0.8*prior + 0.2*freshness
    f0 = ranked[0]["freshness"]
    assert abs(ranked[0]["final"] - (0.8 * 1.0 + 0.2 * f0)) < 1e-9


def test_blend_mode_time_weight_capped():
    """time_weight=1.0 也只按 0.2 参与（相关度主导）。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("old", 1.0, exif_dt="2000-01-01T00:00:00Z"),
        _asset("new", 0.0, exif_dt="2023-12-31T00:00:00Z"),
    ]
    ranked = rank_results(
        assets,
        mode="blend",
        time_weight=1.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    # rank0=old 的 prior=1.0 但 freshness≈0 → 0.8；rank1=new 的 prior≈0.984 但 freshness≈1
    # 0.8*0.984+0.2*1 ≈ 0.987 > 0.8 → new 仍第一，说明时间权重被限制在 0.2
    assert ranked[0]["asset"]["id"] == "new"
    assert ranked[0]["final"] > ranked[1]["final"]


def test_blend_mode_prior_dominates_id_order():
    """w=0 时 final=RRF prior，仅由名次决定，与 id 顺序无关。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        _asset("b", 1.0, exif_dt="2023-06-15T10:00:00Z"),
        _asset("a", 1.0, exif_dt="2023-06-15T10:00:00Z"),
    ]
    ranked = rank_results(
        assets,
        mode="blend",
        time_weight=0.0,
        half_life_days=365.0,
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    # rank0=b 的 prior=1.0 > rank1=a 的 60/61 → b 在前，不按 id 重排
    assert [r["asset"]["id"] for r in ranked] == ["b", "a"]


def test_blend_mode_missing_score_similarity_none():
    """blend 模式下无 score 仍不反推相似度（回归守卫 R2/R5）。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assets = [
        {"id": "only", "fileCreatedAt": None, "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"}},
    ]
    ranked = rank_results(
        assets,
        mode="blend",
        default_iso="1970-01-01T00:00:00+00:00",
        now=now,
    )
    assert ranked[0]["similarity"] is None
    assert ranked[0]["final"] is not None  # final 来自 RRF+freshness，与 similarity 无关
