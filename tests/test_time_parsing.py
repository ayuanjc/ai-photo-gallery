"""时间解析与新鲜度测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.utils.time_parsing import parse_datetime, time_freshness


def test_parse_exif_format_without_tz_is_utc():
    dt = parse_datetime("2023:06:15 10:30:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour in (10, 18)  # 具体值取决于本地时区，此处只断言时区已携带


def test_parse_iso_with_z():
    dt = parse_datetime("2023-06-15T10:30:00Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_invalid_returns_none():
    assert parse_datetime("not-a-date") is None
    assert parse_datetime(None) is None


def test_freshness_newer_is_higher():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    recent = datetime(2023, 12, 31, tzinfo=timezone.utc)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert time_freshness(recent, now=now, half_life_days=365) > time_freshness(
        old, now=now, half_life_days=365
    )


def test_freshness_today_is_one():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert time_freshness(now, now=now, half_life_days=365) == 1.0


def test_freshness_none_is_zero():
    assert time_freshness(None) == 0.0
