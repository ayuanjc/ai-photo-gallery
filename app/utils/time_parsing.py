"""EXIF / 文件创建时间解析与时间新鲜度计算工具。"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# Immich 的 fileCreatedAt / updatedAt / localDateTime 常见时间格式集合。
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y:%m:%d %H:%M:%S",
)

DEFAULT_SOURCE = "default"


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """将多种格式的时间字符串解析为带时区的 datetime。

    - 无时区信息时按 UTC 处理（Immich 的 fileCreatedAt 通常为 UTC）。
    - 解析失败返回 None。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        dt = None
        for fmt in _DATETIME_FORMATS:
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            # 最后尝试 ISO 自动解析
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def time_freshness(
    captured_at: datetime | None,
    now: datetime | None = None,
    half_life_days: float = 365.0,
) -> float:
    """基于指数衰减计算时间新鲜度，取值 (0, 1]。

    freshness = 2 ** (-age / half_life_days)
    - 刚拍摄（age=0）为 1.0，随时间按半衰期衰减，永不小于 0（下限由调用方裁切）。
    - captured_at 为 None（含取回退默认值后极老的时间）会自然趋近 0。

    指数衰减相比线性归一化的优点：单调、有界、对“极老照片”友好，
    不需要对结果集做 min-max 归一化（后者会受单次结果集分布影响，不稳定）。
    """
    now = now or datetime.now(timezone.utc)
    if captured_at is None:
        return 0.0
    age_seconds = (now - captured_at).total_seconds()
    if age_seconds < 0:  # 未来时间当作“最新”
        age_seconds = 0.0
    half_life_seconds = max(half_life_days, 1e-6) * 86400.0
    return float(2 ** (-age_seconds / half_life_seconds))