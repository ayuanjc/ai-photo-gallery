"""统计相关路由。

提供器材偏好统计接口（GET /api/v1/stats/gear），用于可视化大屏。

特性：
- 只读接口，不写数据。
- 走现有 API Key 鉴权 + 令牌桶限流。
- 结果走 CacheBackend（app.state.cache），TTL 由 STATS_CACHE_TTL_SECONDS 控制。
- 上游失败降级：返回 {total: 0, degraded: true, warnings: [...]}，状态码 200。
- STATS_ENABLED=false 返回 503 + SERVICE_UNAVAILABLE。
- 优先使用 StatsProvider（feature/stats-provider 分支），不可用时自行 fallback 分页拉取。
- 侧载资产支持：缺器材信息不计入 top 系列但计入 total；缺拍摄时间不计入 shot_by_month。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...core.config import Settings
from ...core.errors import ServiceUnavailableError, UpstreamError
from ...models.stats import (
    BrandRatioItem,
    FavoriteCombo,
    GearStatsResponse,
    GearTopItem,
    MonthCount,
)
from ..deps import get_settings_dep, rate_limit_guard, verify_api_key

logger = logging.getLogger(__name__)

# 尝试导入 StatsProvider（可能在 feature/stats-provider 分支，未必合入 main）
try:
    from ...services.stats_provider import StatsProvider

    _HAS_STATS_PROVIDER = True
except ImportError:
    StatsProvider = None  # type: ignore[assignment, misc]
    _HAS_STATS_PROVIDER = False

router = APIRouter(
    tags=["stats"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit_guard)],
)

_TOP_LIMIT = 10
_RECENT_MONTHS = 12


# ============================================================================
# 工具函数
# ============================================================================


def _safe_str(value: Any) -> str:
    """提取非空字符串，缺失/空白时返回空串。"""
    if value is None:
        return ""
    return str(value).strip()


async def _fetch_assets_fallback(
    immich: Any,
    *,
    page_size: int,
    max_items: int,
    date_from: str | None,
    date_to: str | None,
) -> list[dict]:
    """直接分页拉取 Immich 资产（含 EXIF），不依赖 StatsProvider。

    通过 GET /api/assets?withExif=true&take=N&skip=M 分页拉取，直到无更多数据或达 max_items。
    """
    ps = max(1, page_size)
    mi = max(0, max_items)
    assets: list[dict] = []
    skip = 0

    while True:
        if mi > 0 and len(assets) >= mi:
            break
        params: dict[str, Any] = {
            "withExif": "true",
            "isTrashed": "false",
            "take": ps,
            "skip": skip,
        }
        if date_from:
            params["createdAfter"] = date_from
        if date_to:
            params["createdBefore"] = date_to

        resp = await immich._request_with_retry("GET", "/api/assets", params=params)
        if resp.status_code != 200:
            raise UpstreamError(
                "Immich 获取资产列表失败",
                details={"status": resp.status_code},
            )
        data = immich._parse_json(resp)
        items = immich._extract_items(data)
        if not items:
            break
        assets.extend(items)
        if len(items) < ps:
            break
        skip += ps

    if mi > 0:
        assets = assets[:mi]
    return assets


def _combine_camera(make: str, model: str) -> str:
    """拼接相机名称：都有 → 'make model'，否则返回非空的那个。"""
    if make and model:
        return f"{make} {model}"
    return make or model


def _extract_month(value: Any) -> str | None:
    """从拍摄时间提取 'YYYY-MM'，格式异常返回 None。

    兼容两种开头：ISO（YYYY-MM-DD...）与 EXIF（YYYY:MM:DD ...）。
    """
    s = _safe_str(value)
    if len(s) < 7:
        return None
    year = s[0:4]
    sep = s[4:5]
    month = s[5:7]
    if sep not in ("-", ":"):
        return None
    if not (year.isdigit() and month.isdigit()):
        return None
    m = int(month)
    if not 1 <= m <= 12:
        return None
    return f"{year}-{month}"


def _recent_month_keys(now: datetime, limit: int = _RECENT_MONTHS) -> set[str]:
    """生成最近 limit 个月（含当前月）的 'YYYY-MM' key 集合。"""
    keys: set[str] = set()
    year, month = now.year, now.month
    for _ in range(limit):
        keys.add(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return keys


def _filter_recent_months(
    month_counts: Counter, now: datetime, limit: int = _RECENT_MONTHS
) -> list[MonthCount]:
    """过滤出最近 limit 个月的月度分布，按月份升序（只含有数据的月份）。"""
    recent = _recent_month_keys(now, limit)
    return [
        MonthCount(month=k, count=v)
        for k, v in sorted(month_counts.items())
        if k in recent
    ]


def _calc_ratio(count: int, total: int) -> float:
    """计算占比，total <= 0 时返回 0（防除零），保留 2 位小数。"""
    if total <= 0:
        return 0.0
    return round(count / total, 2)


# ============================================================================
# 器材聚合
# ============================================================================


def _aggregate_gear(
    assets: list[dict],
    warnings: list[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """聚合器材偏好统计。

    - total 为资产总数（含无器材信息资产）。
    - top_cameras / top_lenses / brand_ratio 只统计有器材信息的资产。
    - ratio = count / total，保留 2 位小数，total=0 时不除零。
    - shot_by_month 只统计有拍摄时间的资产，且只返最近 12 个月。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    total = len(assets)
    camera_counts: Counter = Counter()
    lens_counts: Counter = Counter()
    combo_counts: Counter = Counter()
    brand_counts: Counter = Counter()
    month_counts: Counter = Counter()
    no_gear_count = 0

    for asset in assets:
        exif = asset.get("exifInfo")
        if not isinstance(exif, dict):
            exif = {}

        make = _safe_str(exif.get("make"))
        model = _safe_str(exif.get("model"))
        lens = _safe_str(exif.get("lensModel"))
        camera = _combine_camera(make, model)

        has_gear = bool(make) or bool(model) or bool(lens)
        if not has_gear:
            no_gear_count += 1

        if camera:
            camera_counts[camera] += 1
        if lens:
            lens_counts[lens] += 1
        if camera and lens:
            combo_counts[(camera, lens)] += 1
        if make:
            brand_counts[make] += 1

        month = _extract_month(exif.get("dateTimeOriginal"))
        if month:
            month_counts[month] += 1

    if no_gear_count > 0:
        warnings.append(f"{no_gear_count} 个资产无器材信息（可能为侧载文件），已跳过器材统计")

    top_cameras = [
        GearTopItem(name=k, count=v, ratio=_calc_ratio(v, total))
        for k, v in camera_counts.most_common(_TOP_LIMIT)
    ]
    top_lenses = [
        GearTopItem(name=k, count=v, ratio=_calc_ratio(v, total))
        for k, v in lens_counts.most_common(_TOP_LIMIT)
    ]
    brand_ratio = [
        BrandRatioItem(brand=k, count=v, ratio=_calc_ratio(v, total))
        for k, v in brand_counts.most_common()
    ]

    favorite_combo: FavoriteCombo | None = None
    if combo_counts:
        (camera, lens), count = combo_counts.most_common(1)[0]
        favorite_combo = FavoriteCombo(camera=camera, lens=lens, count=count)

    shot_by_month = _filter_recent_months(month_counts, now)

    return {
        "total": total,
        "top_cameras": top_cameras,
        "top_lenses": top_lenses,
        "favorite_combo": favorite_combo,
        "brand_ratio": brand_ratio,
        "shot_by_month": shot_by_month,
    }


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "/stats/gear",
    response_model=GearStatsResponse,
    summary="器材偏好统计",
    description="聚合图库资产的常用相机、镜头、相机+镜头组合、品牌占比、近 12 个月月度分布，用于可视化大屏。",
)
async def gear_stats(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    date_from: str | None = Query(default=None, description="起始日期（ISO 8601）"),
    date_to: str | None = Query(default=None, description="结束日期（ISO 8601）"),
) -> GearStatsResponse:
    stats_enabled = getattr(settings, "stats_enabled", True)
    if not stats_enabled:
        raise ServiceUnavailableError("统计功能未启用（STATS_ENABLED=false）")

    cache_ttl = getattr(settings, "stats_cache_ttl_seconds", 600)
    max_items = getattr(settings, "stats_max_items", 10000)
    page_size = getattr(settings, "stats_page_size", 100)

    cache_key = f"stats:gear:{date_from or ''}:{date_to or ''}"
    cache = getattr(request.app.state, "cache", None)

    # 尝试读缓存
    if cache is not None:
        try:
            cached = await cache.get(cache_key)
            if cached is not None:
                return GearStatsResponse(**cached)
        except Exception:
            logger.warning("器材统计缓存读取失败，跳过", exc_info=True)

    warnings: list[str] = []
    degraded = False
    assets: list[dict] = []

    immich = request.app.state.immich_client

    try:
        if _HAS_STATS_PROVIDER:
            provider = StatsProvider(settings, immich)
            async for asset in provider.iter_assets():
                assets.append(asset)
        else:
            assets = await _fetch_assets_fallback(
                immich,
                page_size=page_size,
                max_items=max_items,
                date_from=date_from,
                date_to=date_to,
            )
    except Exception as exc:
        logger.warning("器材统计拉取上游失败，降级返回空结果: %s", exc)
        degraded = True
        warnings.append(f"上游拉取失败: {type(exc).__name__}: {exc}")

    if degraded:
        response = GearStatsResponse(total=0, degraded=True, warnings=warnings)
    else:
        agg = _aggregate_gear(assets, warnings)
        response = GearStatsResponse(
            total=agg["total"],
            degraded=False,
            warnings=warnings,
            top_cameras=agg["top_cameras"],
            top_lenses=agg["top_lenses"],
            favorite_combo=agg["favorite_combo"],
            brand_ratio=agg["brand_ratio"],
            shot_by_month=agg["shot_by_month"],
        )

    # 写缓存（降级结果不缓存）
    if cache is not None and not degraded:
        try:
            await cache.set(cache_key, response.model_dump(), ttl=cache_ttl)
        except Exception:
            logger.warning("器材统计缓存写入失败，跳过", exc_info=True)

    return response