"""搜索结果去重：连拍/相似图只保留一张（优先保留 final_score 最高的）。

去重信号（按优先级）：
    1. 同一 asset_id：直接去重
    2. EXIF 拍摄时间相近（默认 3 秒内）+ 尺寸相同 → 视为连拍
    3. 画面相似度：相似度分数超过阈值 + 尺寸相同 → 视为重复

保留策略：同组内保留 final_score 最高的一张。
输入要求：items 已按 final_score 降序排列（保证第一个出现的即是最高分）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def dedup_items(
    items: list[dict[str, Any]],
    time_window_sec: int = 3,
    similarity_threshold: float = 0.95,
) -> tuple[list[dict[str, Any]], int]:
    """对已排序的搜索结果做去重。

    Args:
        items: 已按 final_score 降序排列的结果列表。每项为 dict，包含：
            - asset: 原始 asset dict（含 id、exifInfo 等）
            - final_score: 最终排序分数
            - similarity: 语义相似度分数
            - captured_at: 拍摄时间（datetime | None）
        time_window_sec: 拍摄时间窗口（秒），在此范围内视为连拍。
        similarity_threshold: 相似度阈值，超过此值且尺寸相同视为重复。

    Returns:
        (去重后的 items, 被移除数量)
    """
    if not items:
        return [], 0

    # 防御性参数处理
    window = max(0, time_window_sec)
    threshold = max(0.0, min(1.0, similarity_threshold))

    kept: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    removed = 0

    for item in items:
        asset = item.get("asset") or {}
        asset_id = str(asset.get("id", ""))

        # 信号 1：同一 asset_id
        if asset_id and asset_id in seen_ids:
            removed += 1
            continue

        # 与已保留的 items 逐对比较
        is_dup = False
        for kept_item in kept:
            if _is_duplicate(item, kept_item, window, threshold):
                is_dup = True
                break

        if is_dup:
            removed += 1
            continue

        if asset_id:
            seen_ids.add(asset_id)
        kept.append(item)

    return kept, removed


def _is_duplicate(
    a: dict[str, Any],
    b: dict[str, Any],
    time_window_sec: int,
    similarity_threshold: float,
) -> bool:
    """判断两条结果是否为重复。

    既然 items 已按 final_score 降序排列，a 的分数 >= b 的分数。
    判定为重复时保留 a（先出现的），移除 b。
    但此函数只判断是否重复，保留逻辑由调用方处理。
    """
    asset_a = a.get("asset") or {}
    asset_b = b.get("asset") or {}

    # 信号 1：同一 asset_id
    id_a = str(asset_a.get("id", ""))
    id_b = str(asset_b.get("id", ""))
    if id_a and id_a == id_b:
        return True

    # 获取尺寸
    dims_a = _get_dimensions(asset_a)
    dims_b = _get_dimensions(asset_b)

    # 信号 2：拍摄时间相近 + 尺寸相同 → 连拍
    if time_window_sec > 0:
        time_a = a.get("captured_at")
        time_b = b.get("captured_at")
        if time_a is not None and time_b is not None:
            diff_sec = abs(_to_timestamp(time_a) - _to_timestamp(time_b))
            if diff_sec <= time_window_sec and dims_a == dims_b and dims_a is not None:
                return True

    # 信号 3：相似度分数超过阈值 + 尺寸相同 → 重复
    sim_a = _safe_float(a.get("similarity", 0.0))
    sim_b = _safe_float(b.get("similarity", 0.0))
    if (
        sim_a >= similarity_threshold
        and sim_b >= similarity_threshold
        and dims_a is not None
        and dims_a == dims_b
    ):
        return True

    return False


def _get_dimensions(asset: dict[str, Any]) -> tuple[int, int] | None:
    """从 asset 的 exifInfo 提取（宽, 高），缺失任一返回 None。"""
    exif = asset.get("exifInfo") or {}
    width = exif.get("exifImageWidth")
    height = exif.get("exifImageHeight")
    if width is None or height is None:
        return None
    try:
        return int(width), int(height)
    except (TypeError, ValueError):
        return None


def _to_timestamp(dt: datetime | float | int) -> float:
    """将 datetime 或数值转为 Unix 时间戳（秒）。"""
    if isinstance(dt, datetime):
        return dt.timestamp()
    try:
        return float(dt)
    except (TypeError, ValueError):
        return 0.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全转 float，非数字返回 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default