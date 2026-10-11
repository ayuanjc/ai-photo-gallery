"""二次排序算法：EXIF 拍摄时间与语义相似度的加权融合。

融合公式（权重已归一化）：
    final_score = α * time_freshness + β * similarity_score

- similarity_score 归一化到 [0, 1]（Immich 返回分数若超出该区间会被裁切）。
- time_freshness 由指数衰减得到，见 utils/time_parsing.py。
- 拍摄时间来源优先级：EXIF dateTimeOriginal > fileCreatedAt > 默认时间。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models.response import SearchResultItem
from ..utils.time_parsing import DEFAULT_SOURCE, parse_datetime, time_freshness

logger = logging.getLogger(__name__)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def resolve_captured_at(asset: dict, default_iso: str) -> tuple[datetime | None, str]:
    """按优先级解析拍摄时间，返回 (时间, 来源)。"""
    exif = asset.get("exifInfo") or {}
    exif_datetime = parse_datetime(exif.get("dateTimeOriginal"))
    if exif_datetime is not None:
        return exif_datetime, "exif"

    file_created = parse_datetime(asset.get("fileCreatedAt"))
    if file_created is not None:
        return file_created, "file_created"

    default_dt = parse_datetime(default_iso)
    return default_dt, DEFAULT_SOURCE


def rank_assets(
    assets: list[dict],
    *,
    time_weight: float,
    similarity_weight: float,
    half_life_days: float,
    default_iso: str,
    now: datetime | None = None,
) -> list[tuple[dict, float, float, float, datetime | None, str]]:
    """对 Immich 返回的资产列表做加权融合排序。

    返回按 final_score 降序排列的元组列表：
        (asset, final_score, similarity_score, time_freshness, captured_at, captured_at_source)
    """
    now = now or datetime.now(timezone.utc)
    scored: list[tuple[dict, float, float, float, datetime | None, str]] = []

    for asset in assets:
        similarity = clamp(float(asset.get("score", 1.0) or 0.0))
        captured_at, source = resolve_captured_at(asset, default_iso)
        freshness = time_freshness(captured_at, now=now, half_life_days=half_life_days)
        final = time_weight * freshness + similarity_weight * similarity
        scored.append((asset, final, similarity, freshness, captured_at, source))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored