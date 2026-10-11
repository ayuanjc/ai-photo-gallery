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


from ..utils.time_parsing import DEFAULT_SOURCE, parse_datetime, time_freshness

logger = logging.getLogger(__name__)

# 各主场景动态排序权重表（alpha=时间权重，beta=语义权重，和为 1）。
# 未命中主场景时回退到调用方传入的默认权重（来源为环境变量 time_weight / similarity_weight）。
SCENE_WEIGHTS: dict[str, dict[str, float]] = {
    "people": {"alpha": 0.5, "beta": 0.5},
    "nature": {"alpha": 0.2, "beta": 0.8},
    "event": {"alpha": 0.4, "beta": 0.6},
}


def blend_scene_weights(
    scenes: list[str],
    default_alpha: float,
    default_beta: float,
) -> tuple[float, float]:
    """多场景权重加权综合：对 scenes 中每个场景取 SCENE_WEIGHTS 的 (alpha, beta) 算术平均。

    - 不在 SCENE_WEIGHTS 中的场景（含 "default" 标识）跳过。
    - scenes 为空或过滤后为空时回退 default_alpha / default_beta。
    - 平均后做归一化，保证 alpha + beta == 1。
    """
    alphas: list[float] = []
    betas: list[float] = []
    for scene in scenes or []:
        entry = SCENE_WEIGHTS.get(scene) if scene else None
        if entry is None or scene == "default":
            continue
        alphas.append(entry["alpha"])
        betas.append(entry["beta"])

    if not alphas:
        return default_alpha, default_beta

    alpha = sum(alphas) / len(alphas)
    beta = sum(betas) / len(betas)
    total = alpha + beta
    if total <= 0:
        return default_alpha, default_beta
    return alpha / total, beta / total


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

    每条 asset 的融合权重按其 scenes 多场景综合选择（见 blend_scene_weights），
    scenes 为空时回退到 primary_scene 单场景，再为空则用传入的默认权重。

    返回按 final_score 降序排列的元组列表：
        (asset, final_score, similarity_score, time_freshness, captured_at, captured_at_source)
    """
    now = now or datetime.now(timezone.utc)
    scored: list[tuple[dict, float, float, float, datetime | None, str]] = []

    for asset in assets:
        # 按 asset 的多场景综合选权重；scenes 为空时回退到 primary_scene 单场景兜底。
        alpha, beta = blend_scene_weights(
            asset.get("scenes") or [asset.get("primary_scene")],
            default_alpha=time_weight,
            default_beta=similarity_weight,
        )
        similarity = clamp(float(asset.get("score", 1.0) or 0.0))
        captured_at, source = resolve_captured_at(asset, default_iso)
        freshness = time_freshness(captured_at, now=now, half_life_days=half_life_days)
        final = alpha * freshness + beta * similarity
        scored.append((asset, final, similarity, freshness, captured_at, source))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored