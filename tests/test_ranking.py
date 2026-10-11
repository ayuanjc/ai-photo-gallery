"""二次排序算法测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.ranking import clamp, rank_assets, resolve_captured_at


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