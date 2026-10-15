"""统计相关路由。

提供地图足迹统计接口（GET /api/v1/stats/map）与 EXIF 元数据分布统计接口
（GET /api/v1/stats/exif），用于可视化大屏。

特性：
- 只读接口，不写数据。
- 走现有 API Key 鉴权 + 令牌桶限流。
- 结果走 CacheBackend（app.state.cache），TTL 由 STATS_CACHE_TTL_SECONDS 控制。
- 上游失败降级：返回 {total: 0, degraded: true, warnings: [...]}，状态码 200。
- STATS_ENABLED=false 返回 503 + SERVICE_UNAVAILABLE。
- 优先使用 StatsProvider（feature/stats-provider 分支），不可用时自行 fallback 分页拉取。
- 地图：无 GPS 资产不计入；GPS 格式异常跳过并记 warnings；隐私 asset_ids 默认不返回。
- EXIF：字段名对齐 Immich：make / model / lensModel / fNumber / iso / focalLength / exposureTime。
- EXIF：缺失 EXIF 字段跳过对应分布但计入 total，记录 warnings 不报错。
"""
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...core.config import Settings
from ...core.errors import ServiceUnavailableError, UpstreamError
from ...core.runtime_config import runtime_enabled
from ...utils.date_parse import parse_date_param
from ...models.stats import (
    CityStat,
    CountryStat,
    ExifStatsResponse,
    MapPoint,
    MapStatsResponse,
    NameCount,
    ValueCount,
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

_MAX_RAW_POINTS = 2000
_TOP_N = 20
_TOP_LIMIT = 10
_RECENT_MONTHS = 12


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


# ============================================================================
# EXIF 值格式化
# ============================================================================


def _format_aperture(value: Any) -> str | None:
    """格式化光圈值：2.8 → f/2.8。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f"f/{f:g}"


def _format_iso(value: Any) -> str | None:
    """格式化 ISO：100 → ISO 100。"""
    if value is None:
        return None
    try:
        i = int(value)
    except (TypeError, ValueError):
        return None
    if i <= 0:
        return None
    return f"ISO {i}"


def _format_focal_length(value: Any) -> str | None:
    """格式化焦距：50 → 50mm。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f"{f:g}mm"


def _format_shutter_speed(value: Any) -> str | None:
    """格式化快门速度：0.01 → 1/100，2.0 → 2s。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    if f < 1:
        denom = round(1 / f)
        if denom <= 0:
            return None
        return f"1/{denom}"
    return f"{f:g}s"


# ============================================================================
# Fallback 分页拉取（StatsProvider 不可用时）
# ============================================================================


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

        data = await immich.request_json("GET", "/api/assets", params=params)
        items = immich.extract_items(data)
        if not items:
            break
        assets.extend(items)
        if len(items) < ps:
            break
        skip += ps

    if mi > 0:
        assets = assets[:mi]
    return assets


# ============================================================================


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
# 地图聚合
# ============================================================================


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
# EXIF 聚合
# ============================================================================


def _aggregate_exif(assets: list[dict]) -> dict[str, Any]:
    """聚合 EXIF 分布统计。

    遍历资产列表，提取 exifInfo 中的字段，按 top N 统计分布。
    无 exifInfo 或缺失字段的资产计入 total 但不计入对应分布。
    """
    total = len(assets)
    exif_count = 0
    brands: Counter = Counter()
    cameras: Counter = Counter()
    lenses: Counter = Counter()
    apertures: Counter = Counter()
    isos: Counter = Counter()
    focal_lengths: Counter = Counter()
    shutters: Counter = Counter()

    for asset in assets:
        exif = asset.get("exifInfo")
        if not isinstance(exif, dict):
            continue
        exif_count += 1

        make = _safe_str(exif.get("make"))
        model = _safe_str(exif.get("model"))
        lens = _safe_str(exif.get("lensModel"))

        if make:
            brands[make] += 1
        if make and model:
            cameras[f"{make} {model}"] += 1
        elif model:
            cameras[model] += 1
        if lens:
            lenses[lens] += 1

        aperture = _format_aperture(exif.get("fNumber"))
        if aperture:
            apertures[aperture] += 1
        iso = _format_iso(exif.get("iso"))
        if iso:
            isos[iso] += 1
        focal = _format_focal_length(exif.get("focalLength"))
        if focal:
            focal_lengths[focal] += 1
        shutter = _format_shutter_speed(exif.get("exposureTime"))
        if shutter:
            shutters[shutter] += 1

    def top_name(counter: Counter) -> list[NameCount]:
        return [NameCount(name=k, count=v) for k, v in counter.most_common(_TOP_N)]

    def top_value(counter: Counter) -> list[ValueCount]:
        return [ValueCount(value=k, count=v) for k, v in counter.most_common(_TOP_N)]

    return {
        "total": total,
        "exif_count": exif_count,
        "camera_brands": top_name(brands),
        "cameras": top_name(cameras),
        "lenses": top_name(lenses),
        "aperture_distribution": top_value(apertures),
        "iso_distribution": top_value(isos),
        "focal_length_distribution": top_value(focal_lengths),
        "shutter_speed_distribution": top_value(shutters),
    }


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
    stats_enabled = runtime_enabled(
        "stats_enabled", getattr(settings, "stats_enabled", True)
    )
    if not stats_enabled:
        raise ServiceUnavailableError("统计功能未启用（STATS_ENABLED=false）")

    date_from_dt = parse_date_param(date_from, field_name="date_from")
    date_to_dt = parse_date_param(date_to, field_name="date_to")

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
            assets, provider_warnings = await provider.fetch_assets(
                page_size=page_size,
                max_items=max_items,
                date_from=date_from_dt,
                date_to=date_to_dt,
            )
            if provider_warnings:
                degraded = True
                warnings.append("上游拉取失败: " + "; ".join(provider_warnings))
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


@router.get(
    "/stats/exif",
    response_model=ExifStatsResponse,
    summary="EXIF 统计分布",
    description="聚合图库资产的 EXIF 元数据分布（相机品牌、镜头、光圈、ISO、焦距、快门速度），用于可视化大屏。",
)
async def exif_stats(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    date_from: str | None = Query(default=None, description="起始日期（ISO 8601）"),
    date_to: str | None = Query(default=None, description="结束日期（ISO 8601）"),
) -> ExifStatsResponse:
    stats_enabled = runtime_enabled(
        "stats_enabled", getattr(settings, "stats_enabled", True)
    )
    if not stats_enabled:
        raise ServiceUnavailableError("统计功能未启用（STATS_ENABLED=false）")

    date_from_dt = parse_date_param(date_from, field_name="date_from")
    date_to_dt = parse_date_param(date_to, field_name="date_to")

    cache_ttl = getattr(settings, "stats_cache_ttl_seconds", 600)
    max_items = getattr(settings, "stats_max_items", 10000)
    page_size = getattr(settings, "stats_page_size", 100)

    cache_key = f"stats:exif:{date_from or ''}:{date_to or ''}"
    cache = getattr(request.app.state, "cache", None)

    # 尝试读缓存
    if cache is not None:
        try:
            cached = await cache.get(cache_key)
            if cached is not None:
                return ExifStatsResponse(**cached)
        except Exception:
            logger.warning("EXIF 统计缓存读取失败，跳过", exc_info=True)

    warnings: list[str] = []
    degraded = False
    assets: list[dict] = []

    immich = request.app.state.immich_client

    try:
        if _HAS_STATS_PROVIDER:
            provider = StatsProvider(settings, immich)
            assets, provider_warnings = await provider.fetch_assets(
                page_size=page_size,
                max_items=max_items,
                date_from=date_from_dt,
                date_to=date_to_dt,
            )
            if provider_warnings:
                degraded = True
                warnings.append("上游拉取失败: " + "; ".join(provider_warnings))
        else:
            assets = await _fetch_assets_fallback(
                immich,
                page_size=page_size,
                max_items=max_items,
                date_from=date_from,
                date_to=date_to,
            )
    except Exception as exc:
        logger.warning("EXIF 统计拉取上游失败，降级返回空结果: %s", exc)
        degraded = True
        warnings.append(f"上游拉取失败: {type(exc).__name__}: {exc}")

    if degraded:
        response = ExifStatsResponse(total=0, degraded=True, warnings=warnings)
    else:
        agg = _aggregate_exif(assets)
        if agg["total"] > 0 and agg["exif_count"] < agg["total"]:
            no_exif_count = agg["total"] - agg["exif_count"]
            warnings.append(f"{no_exif_count} 个资产无 EXIF 信息（可能为侧载文件）")
        response = ExifStatsResponse(
            total=agg["total"],
            degraded=False,
            warnings=warnings,
            camera_brands=agg["camera_brands"],
            cameras=agg["cameras"],
            lenses=agg["lenses"],
            aperture_distribution=agg["aperture_distribution"],
            iso_distribution=agg["iso_distribution"],
            focal_length_distribution=agg["focal_length_distribution"],
            shutter_speed_distribution=agg["shutter_speed_distribution"],
        )

    # 写缓存（降级结果不缓存）
    if cache is not None and not degraded:
        try:
            await cache.set(cache_key, response.model_dump(), ttl=cache_ttl)
        except Exception:
            logger.warning("EXIF 统计缓存写入失败，跳过", exc_info=True)

    return response

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
    stats_enabled = runtime_enabled(
        "stats_enabled", getattr(settings, "stats_enabled", True)
    )
    if not stats_enabled:
        raise ServiceUnavailableError("统计功能未启用（STATS_ENABLED=false）")

    date_from_dt = parse_date_param(date_from, field_name="date_from")
    date_to_dt = parse_date_param(date_to, field_name="date_to")

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
            assets, provider_warnings = await provider.fetch_assets(
                page_size=page_size,
                max_items=max_items,
                date_from=date_from_dt,
                date_to=date_to_dt,
            )
            if provider_warnings:
                degraded = True
                warnings.append("上游拉取失败: " + "; ".join(provider_warnings))
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