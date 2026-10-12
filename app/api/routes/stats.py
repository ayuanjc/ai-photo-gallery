"""统计相关路由。

提供地图足迹统计接口（GET /api/v1/stats/map），用于可视化大屏。

特性：
- 只读接口，不写数据。
- 走现有 API Key 鉴权 + 令牌桶限流。
- 结果走 CacheBackend（app.state.cache），TTL 由 STATS_CACHE_TTL_SECONDS 控制。
- 上游失败降级：返回 {total_with_gps: 0, degraded: true, warnings: [...]}，状态码 200。
- STATS_ENABLED=false 返回 503 + SERVICE_UNAVAILABLE。
- 优先使用 StatsProvider（feature/stats-provider 分支），不可用时自行 fallback 分页拉取。
- 侧载资产支持：无 GPS 资产不计入；GPS 格式异常跳过并记 warnings。
- 隐私：asset_ids 默认不返回，需 show_ids=true。
"""
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...core.config import Settings
from ...core.errors import ServiceUnavailableError, UpstreamError
from ...models.stats import CityStat, CountryStat, MapPoint, MapStatsResponse
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

_MAX_RAW_POINTS = 2000


# ============================================================================
# 工具函数
# ============================================================================


def _safe_str(value: Any) -> str:
    """提取非空字符串，缺失/空白时返回空串。"""
    if value is None:
        return ""
    s = str(value).strip()
    return s


def _parse_latlng(lat: Any, lng: Any) -> tuple[float, float] | None:
    """校验并解析经纬度，非法返回 None。

    合法范围：纬度 [-90, 90]，经度 [-180, 180]，拒绝 NaN/Infinity/非数字。
    """
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return None
    if math.isnan(lat_f) or math.isnan(lng_f):
        return None
    if math.isinf(lat_f) or math.isinf(lng_f):
        return None
    if not -90.0 <= lat_f <= 90.0:
        return None
    if not -180.0 <= lng_f <= 180.0:
        return None
    return lat_f, lng_f


async def _fetch_assets_fallback(
    immich: Any,
    *,
    page_size: int,
    max_items: int,
    date_from: str | None,
    date_to: str | None,
) -> list[dict]:
    """直接分页拉取 Immich 资产（含 EXIF/GPS），不依赖 StatsProvider。

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


def _extract_gps(asset: dict) -> tuple[Any, Any]:
    """提取资产的经纬度原始值，优先 exifInfo，回退资产顶层。"""
    exif = asset.get("exifInfo")
    if isinstance(exif, dict):
        lat = exif.get("latitude")
        lng = exif.get("longitude")
        if lat is not None or lng is not None:
            return lat, lng
    return asset.get("latitude"), asset.get("longitude")


def _extract_place(asset: dict) -> tuple[str, str]:
    """提取资产的城市/国家（清洗为字符串）。"""
    exif = asset.get("exifInfo")
    city = ""
    country = ""
    if isinstance(exif, dict):
        city = _safe_str(exif.get("city"))
        country = _safe_str(exif.get("country"))
    return city, country


# ============================================================================
# 地图聚合
# ============================================================================


def _aggregate_map(
    assets: list[dict],
    *,
    cluster: str,
    show_ids: bool,
    warnings: list[str],
) -> dict[str, Any]:
    """聚合地图足迹统计。

    遍历资产，提取有效 GPS 坐标、城市、国家：
    - 无 GPS 资产完全忽略。
    - GPS 格式异常（字段缺失半边、超出范围、NaN/Infinity）跳过并记 warnings。
    - cluster=city：返回城市聚合；cluster=raw：返回原始点（最多 2000 条）。
    - countries 始终返回国家聚合。
    """
    total_with_gps = 0
    gps_invalid_count = 0

    # 坐标 -> 资产 id 列表（cluster=raw 去重用）
    coord_assets: dict[tuple[float, float], list[str]] = defaultdict(list)
    # 城市 key -> 聚合信息
    city_agg: dict[tuple[str, str], dict[str, Any]] = {}
    # 国家 -> 计数
    country_counts: Counter = Counter()

    for asset in assets:
        lat, lng = _extract_gps(asset)

        if lat is None and lng is None:
            # 无 GPS（侧载资产常见），忽略
            continue

        parsed = _parse_latlng(lat, lng)
        if parsed is None:
            gps_invalid_count += 1
            continue

        lat_f, lng_f = parsed
        total_with_gps += 1
        asset_id = _safe_str(asset.get("id"))
        coord_assets[(lat_f, lng_f)].append(asset_id)

        city, country = _extract_place(asset)
        city_key = (city, country)
        if city_key not in city_agg:
            city_agg[city_key] = {
                "city": city,
                "country": country,
                "count": 0,
                "lat": lat_f,
                "lng": lng_f,
            }
        city_agg[city_key]["count"] += 1

        if country:
            country_counts[country] += 1

    if gps_invalid_count > 0:
        warnings.append(f"{gps_invalid_count} 个资产 GPS 格式异常，已跳过")

    # 国家聚合（始终返回）
    countries = [
        CountryStat(country=k, count=v) for k, v in country_counts.most_common()
    ]

    # 城市聚合
    cities: list[CityStat] = []
    if cluster == "city":
        sorted_cities = sorted(
            city_agg.values(), key=lambda c: (-c["count"], c["country"], c["city"])
        )
        cities = [
            CityStat(
                city=c["city"],
                country=c["country"],
                count=c["count"],
                lat=c["lat"],
                lng=c["lng"],
            )
            for c in sorted_cities
        ]

    # 原始点（最多 2000 条，按 count 降序）
    points: list[MapPoint] = []
    if cluster == "raw":
        sorted_coords = sorted(
            coord_assets.items(), key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1])
        )
        for (plat, plng), ids in sorted_coords[:_MAX_RAW_POINTS]:
            points.append(
                MapPoint(
                    lat=plat,
                    lng=plng,
                    count=len(ids),
                    asset_ids=ids if show_ids else None,
                )
            )

    return {
        "total_with_gps": total_with_gps,
        "points": points,
        "cities": cities,
        "countries": countries,
    }


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "/stats/map",
    response_model=MapStatsResponse,
    summary="地图足迹统计",
    description="聚合图库带 GPS 资产的照片点、城市聚合、国家聚合，用于可视化大屏。",
)
async def map_stats(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    date_from: str | None = Query(default=None, description="起始日期（ISO 8601）"),
    date_to: str | None = Query(default=None, description="结束日期（ISO 8601）"),
    cluster: str = Query(default="city", description="聚合方式：city | raw"),
    show_ids: bool = Query(default=False, description="是否返回 asset_ids（默认否，隐私保护）"),
) -> MapStatsResponse:
    stats_enabled = getattr(settings, "stats_enabled", True)
    if not stats_enabled:
        raise ServiceUnavailableError("统计功能未启用（STATS_ENABLED=false）")

    if cluster not in ("city", "raw"):
        cluster = "city"

    cache_ttl = getattr(settings, "stats_cache_ttl_seconds", 600)
    max_items = getattr(settings, "stats_max_items", 10000)
    page_size = getattr(settings, "stats_page_size", 100)

    cache_key = f"stats:map:{date_from or ''}:{date_to or ''}:{cluster}:{int(show_ids)}"
    cache = getattr(request.app.state, "cache", None)

    # 尝试读缓存
    if cache is not None:
        try:
            cached = await cache.get(cache_key)
            if cached is not None:
                return MapStatsResponse(**cached)
        except Exception:
            logger.warning("地图统计缓存读取失败，跳过", exc_info=True)

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
        logger.warning("地图统计拉取上游失败，降级返回空结果: %s", exc)
        degraded = True
        warnings.append(f"上游拉取失败: {type(exc).__name__}: {exc}")

    if degraded:
        response = MapStatsResponse(total_with_gps=0, degraded=True, warnings=warnings)
    else:
        agg = _aggregate_map(
            assets,
            cluster=cluster,
            show_ids=show_ids,
            warnings=warnings,
        )
        response = MapStatsResponse(
            total_with_gps=agg["total_with_gps"],
            degraded=False,
            warnings=warnings,
            points=agg["points"],
            cities=agg["cities"],
            countries=agg["countries"],
        )

    # 写缓存（降级结果不缓存）
    if cache is not None and not degraded:
        try:
            await cache.set(cache_key, response.model_dump(), ttl=cache_ttl)
        except Exception:
            logger.warning("地图统计缓存写入失败，跳过", exc_info=True)

    return response