"""统计接口测试。

覆盖地图足迹统计与 EXIF 分布统计两套接口：
- 地图：有 GPS 聚合、cluster=city/raw 区分、show_ids 隐私控制、无 GPS 资产、
  GPS 格式异常、上游降级、STATS_ENABLED=false 503、鉴权、StatsProvider 降级、
  缓存命中、非法 cluster 回退。
- EXIF：正常聚合、无 EXIF 资产、部分无 EXIF（侧载）、空数据、上游失败降级、
  STATS_ENABLED=false 503、无 API Key 401、缓存命中、StatsProvider 不可用 fallback、
  StatsProvider 可用正常工作、不同日期参数缓存隔离。
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_settings_dep
from app.core.cache import MemoryCache
from app.core.errors import UpstreamUnavailableError
from app.main import app


# ============================================================================
# 测试辅助
# ============================================================================


class FakeResponse:
    def __init__(self, status_code: int, data: Any) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> Any:
        return self._data


class FakeImmichClient:
    """模拟 ImmichClient，按 skip/take 参数返回分页数据。"""

    def __init__(self, all_assets: list[dict], status_code: int = 200) -> None:
        self._all_assets = all_assets
        self._status_code = status_code
        self.call_count = 0

    async def _request_with_retry(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
        self.call_count += 1
        if self._status_code != 200:
            raise UpstreamUnavailableError("Immich 服务不可用")
        params = kwargs.get("params", {})
        skip = int(params.get("skip", 0))
        take = int(params.get("take", 100))
        page = self._all_assets[skip : skip + take]
        return FakeResponse(200, page)

    @staticmethod
    def _parse_json(resp: FakeResponse) -> Any:
        return resp._data

    @staticmethod
    def _extract_items(data: Any) -> list[dict]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []


class FailingImmichClient:
    """模拟 ImmichClient 上游不可用。"""

    async def _request_with_retry(self, method: str, path: str, **kwargs: Any) -> Any:
        raise UpstreamUnavailableError("Immich 服务不可用")

    @staticmethod
    def _parse_json(resp: Any) -> Any:
        return {}

    @staticmethod
    def _extract_items(data: Any) -> list[dict]:
        return []


def _asset(
    asset_id: str = "x",
    lat: Any = None,
    lng: Any = None,
    city: str | None = None,
    country: str | None = None,
    make: str | None = None,
    model: str | None = None,
    lens: str | None = None,
    f_number: Any = None,
    iso: Any = None,
    focal_length: Any = None,
    exposure_time: Any = None,
) -> dict:
    """构造测试资产（含 GPS/地点/EXIF 信息）。"""
    exif: dict[str, Any] = {}
    if lat is not None:
        exif["latitude"] = lat
    if lng is not None:
        exif["longitude"] = lng
    if city is not None:
        exif["city"] = city
    if country is not None:
        exif["country"] = country
    if make is not None:
        exif["make"] = make
    if model is not None:
        exif["model"] = model
    if lens is not None:
        exif["lensModel"] = lens
    if f_number is not None:
        exif["fNumber"] = f_number
    if iso is not None:
        exif["iso"] = iso
    if focal_length is not None:
        exif["focalLength"] = focal_length
    if exposure_time is not None:
        exif["exposureTime"] = exposure_time
    asset: dict[str, Any] = {"id": asset_id}
    if exif:
        asset["exifInfo"] = exif
    return asset


def _fake_settings(**kwargs: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = dict(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        search_api_key="",
        redis_enabled=False,
        rate_limit_enabled=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@contextmanager
def make_client(
    *,
    immich: Any = None,
    settings: Any = None,
    cache: Any = None,
):
    """构造测试客户端：注入假 Immich 客户端、设置、缓存。"""
    if settings is None:
        settings = _fake_settings()
    if immich is None:
        immich = FakeImmichClient([])
    if cache is None:
        cache = MemoryCache()

    app.dependency_overrides[get_settings_dep] = lambda: settings
    try:
        with TestClient(app) as c:
            app.state.immich_client = immich
            app.state.rate_limiter = None
            app.state.cache = cache
            yield c
    finally:
        app.dependency_overrides.clear()


def _get_map(client: TestClient, headers: dict | None = None, **params: Any):
    return client.get("/api/v1/stats/map", params=params, headers=headers)


def _get_stats(client: TestClient, headers: dict | None = None, **params: Any):
    return client.get("/api/v1/stats/exif", params=params, headers=headers)


# ============================================================================
# 地图足迹统计 — 1. 有 GPS 资产聚合正确（cluster=city）
# ============================================================================


def test_map_normal_city_aggregation():
    """多个带 GPS 资产，cluster=city，城市与国家聚合正确。"""
    assets = [
        _asset("1", lat=39.9, lng=116.4, city="北京", country="中国"),
        _asset("2", lat=39.9, lng=116.4, city="北京", country="中国"),
        _asset("3", lat=31.2, lng=121.5, city="上海", country="中国"),
        _asset("4", lat=40.7, lng=-74.0, city="New York", country="USA"),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="city")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_with_gps"] == 4
    assert body["degraded"] is False
    assert body["warnings"] == []

    cities = {(x["city"], x["country"]): x for x in body["cities"]}
    assert cities[("北京", "中国")]["count"] == 2
    assert cities[("上海", "中国")]["count"] == 1
    assert cities[("New York", "USA")]["count"] == 1

    countries = {x["country"]: x["count"] for x in body["countries"]}
    assert countries == {"中国": 3, "USA": 1}


# ============================================================================
# 地图 — 2. cluster=city 与 raw 行为区分
# ============================================================================


def test_map_cluster_city_returns_cities_not_points():
    """cluster=city：返回 cities，points 为空。"""
    assets = [_asset("1", lat=39.9, lng=116.4, city="北京", country="中国")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="city")

    body = resp.json()
    assert len(body["cities"]) == 1
    assert body["points"] == []
    assert len(body["countries"]) == 1


def test_map_cluster_raw_returns_points_not_cities():
    """cluster=raw：返回 points，cities 为空。"""
    assets = [_asset("1", lat=39.9, lng=116.4, city="北京", country="中国")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw")

    body = resp.json()
    assert len(body["points"]) == 1
    assert body["cities"] == []
    assert len(body["countries"]) == 1


def test_map_cluster_raw_merges_same_coordinate():
    """cluster=raw：相同坐标合并 count，降序排列。"""
    assets = [
        _asset("1", lat=39.9, lng=116.4),
        _asset("2", lat=39.9, lng=116.4),
        _asset("3", lat=31.2, lng=121.5),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw", show_ids=True)

    body = resp.json()
    assert body["total_with_gps"] == 3
    assert len(body["points"]) == 2
    first = body["points"][0]
    assert first["lat"] == 39.9
    assert first["lng"] == 116.4
    assert first["count"] == 2
    assert sorted(first["asset_ids"]) == sorted(["1", "2"])
    assert body["points"][1]["count"] == 1


# ============================================================================
# 地图 — 3. 默认不返回 asset_ids；4. show_ids=true 返回
# ============================================================================


def test_map_default_no_asset_ids():
    """show_ids 默认 false：points 的 asset_ids 为 null。"""
    assets = [_asset("1", lat=39.9, lng=116.4)]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw")

    body = resp.json()
    assert len(body["points"]) == 1
    assert body["points"][0]["asset_ids"] is None


def test_map_show_ids_true_returns_asset_ids():
    """show_ids=true：points 的 asset_ids 返回资产 ID 列表。"""
    assets = [
        _asset("1", lat=39.9, lng=116.4),
        _asset("2", lat=39.9, lng=116.4),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw", show_ids=True)

    body = resp.json()
    assert body["points"][0]["asset_ids"] == ["1", "2"]


# ============================================================================
# 地图 — 5. 无 GPS 资产不计入
# ============================================================================


def test_map_no_gps_assets_not_counted():
    """无 GPS 资产不计入 total_with_gps 与任何分布。"""
    assets = [
        _asset("1", lat=39.9, lng=116.4, city="北京", country="中国"),
        _asset("2"),  # 无 exifInfo
        _asset("3", city="上海", country="中国"),  # 有地点但无 GPS
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="city")

    body = resp.json()
    assert body["total_with_gps"] == 1
    assert body["warnings"] == []  # 无 GPS 不算格式异常
    cities = {x["city"] for x in body["cities"]}
    assert cities == {"北京"}  # 上海无 GPS 不计入
    countries = {x["country"]: x["count"] for x in body["countries"]}
    assert countries == {"中国": 1}


# ============================================================================
# 地图 — 6. GPS 格式异常：跳过并记 warnings
# ============================================================================


def test_map_gps_out_of_range_warnings():
    """经度超过 ±180：跳过并记 warnings。"""
    assets = [
        _asset("1", lat=39.9, lng=116.4),
        _asset("2", lat=0.0, lng=200.0),  # 经度超范围
        _asset("3", lat=95.0, lng=100.0),  # 纬度超范围
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw")

    body = resp.json()
    assert body["total_with_gps"] == 1
    assert len(body["warnings"]) == 1
    assert "2" in body["warnings"][0]
    assert "GPS" in body["warnings"][0]


def test_map_gps_half_missing_warnings():
    """只有经度没有纬度（字段不完整）：跳过并记 warnings。"""
    assets = [
        _asset("1", lat=39.9, lng=116.4),
        _asset("2", lng=116.4),  # 缺 latitude
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw")

    body = resp.json()
    assert body["total_with_gps"] == 1
    assert len(body["warnings"]) == 1


def test_map_gps_non_numeric_warnings():
    """经纬度为非数字字符串：跳过并记 warnings。"""
    assets = [
        _asset("1", lat="abc", lng="def"),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw")

    body = resp.json()
    assert body["total_with_gps"] == 0
    assert len(body["warnings"]) == 1


# ============================================================================
# 地图 — 7. 上游不可用返回 degraded=true + warnings
# ============================================================================


def test_map_upstream_failure_degraded():
    """Immich 上游不可用：降级返回 total_with_gps=0 + degraded=true。"""
    immich = FailingImmichClient()
    with make_client(immich=immich) as c:
        resp = _get_map(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_with_gps"] == 0
    assert body["degraded"] is True
    assert len(body["warnings"]) == 1
    assert "上游" in body["warnings"][0]


# ============================================================================
# 地图 — 8. STATS_ENABLED=false 返回 503
# ============================================================================


def test_map_stats_disabled_503():
    """STATS_ENABLED=false：返回 503 + SERVICE_UNAVAILABLE。"""
    settings = _fake_settings(stats_enabled=False)
    with make_client(settings=settings) as c:
        resp = _get_map(c)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ============================================================================
# 地图 — 鉴权
# ============================================================================


def test_map_no_api_key_401():
    """配置 search_api_key 但未携带 x-api-key：返回 401。"""
    settings = _fake_settings(search_api_key="secret-key")
    with make_client(settings=settings) as c:
        resp = _get_map(c)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_map_with_api_key_200():
    """携带正确 x-api-key：返回 200。"""
    settings = _fake_settings(search_api_key="secret-key")
    with make_client(settings=settings, immich=FakeImmichClient([])) as c:
        resp = _get_map(c, headers={"x-api-key": "secret-key"})

    assert resp.status_code == 200


# ============================================================================
# 地图 — 9. StatsProvider 降级路径
# ============================================================================


def test_map_stats_provider_unavailable_fallback(monkeypatch):
    """_HAS_STATS_PROVIDER=False 时使用 fallback 分页拉取。"""
    monkeypatch.setattr("app.api.routes.stats._HAS_STATS_PROVIDER", False)

    assets = [_asset("1", lat=39.9, lng=116.4, city="北京", country="中国")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="city")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_with_gps"] == 1
    assert immich.call_count >= 1  # fallback 确实调用了 Immich


class FakeStatsProvider:
    """模拟 StatsProvider，直接返回预设资产。"""

    def __init__(self, settings: Any, immich: Any) -> None:
        self._immich = immich

    async def iter_assets(self):
        for asset in getattr(self._immich, "_all_assets", []):
            yield asset


def test_map_stats_provider_available(monkeypatch):
    """_HAS_STATS_PROVIDER=True 且 StatsProvider 可用时正常聚合。"""
    monkeypatch.setattr("app.api.routes.stats._HAS_STATS_PROVIDER", True)
    monkeypatch.setattr("app.api.routes.stats.StatsProvider", FakeStatsProvider)

    assets = [
        _asset("1", lat=39.9, lng=116.4, city="北京", country="中国"),
        _asset("2", lat=31.2, lng=121.5, city="上海", country="中国"),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="city")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_with_gps"] == 2
    assert len(body["cities"]) == 2


# ============================================================================
# 地图 — 缓存
# ============================================================================


def test_map_cache_hit_no_refetch():
    """第二次相同参数请求命中缓存：不再次调用 Immich。"""
    assets = [_asset("1", lat=39.9, lng=116.4)]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp1 = _get_map(c, cluster="raw")
        assert resp1.status_code == 200
        assert immich.call_count == 1

        resp2 = _get_map(c, cluster="raw")
        assert resp2.status_code == 200
        assert immich.call_count == 1  # 未再次拉取


def test_map_different_cluster_cache_isolation():
    """不同 cluster 参数使用不同缓存 key，分别拉取。"""
    assets = [_asset("1", lat=39.9, lng=116.4)]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp1 = _get_map(c, cluster="city")
        resp2 = _get_map(c, cluster="raw")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert immich.call_count == 2  # 两次不同参数各拉一次


# ============================================================================
# 地图 — 非法 cluster 回退
# ============================================================================


def test_map_invalid_cluster_falls_back_to_city():
    """非法 cluster 值回退为 city，不报错。"""
    assets = [_asset("1", lat=39.9, lng=116.4, city="北京", country="中国")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="foobar")

    body = resp.json()
    assert resp.status_code == 200
    assert len(body["cities"]) == 1
    assert body["points"] == []


# ============================================================================
# EXIF 统计 — 1. 正常聚合正确
# ============================================================================


def test_exif_normal_aggregation():
    """多种相机/镜头/参数的资产正常聚合分布。"""
    assets = [
        _asset(make="Canon", model="EOS R5", lens="RF 24-70", f_number=2.8, iso=100, focal_length=50, exposure_time=0.01),
        _asset(make="Canon", model="EOS R5", lens="RF 24-70", f_number=2.8, iso=200, focal_length=70, exposure_time=0.005),
        _asset(make="Nikon", model="Z7", lens="Z 50mm", f_number=1.8, iso=400, focal_length=50, exposure_time=0.002),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["degraded"] is False
    assert body["warnings"] == []

    brands = {b["name"]: b["count"] for b in body["camera_brands"]}
    assert brands == {"Canon": 2, "Nikon": 1}

    cameras = {c_["name"]: c_["count"] for c_ in body["cameras"]}
    assert cameras == {"Canon EOS R5": 2, "Nikon Z7": 1}

    lenses = {l["name"]: l["count"] for l in body["lenses"]}
    assert lenses == {"RF 24-70": 2, "Z 50mm": 1}

    apertures = {a["value"]: a["count"] for a in body["aperture_distribution"]}
    assert apertures == {"f/2.8": 2, "f/1.8": 1}

    isos = {i["value"]: i["count"] for i in body["iso_distribution"]}
    assert isos == {"ISO 100": 1, "ISO 200": 1, "ISO 400": 1}

    focals = {f["value"]: f["count"] for f in body["focal_length_distribution"]}
    assert focals == {"50mm": 2, "70mm": 1}

    shutters = {s["value"]: s["count"] for s in body["shutter_speed_distribution"]}
    assert shutters == {"1/100": 1, "1/200": 1, "1/500": 1}


# ============================================================================
# EXIF — 2. 无 EXIF 不计入分布但计入 total
# ============================================================================


def test_exif_no_exif_counted_in_total_not_distribution():
    """资产有 exifInfo 但字段全空：计入 total，不计入任何分布。"""
    assets = [
        _asset(),  # 无 exifInfo
        _asset(make="Canon", model="EOS R5", f_number=2.8),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["camera_brands"] == [{"name": "Canon", "count": 1}]
    assert body["cameras"] == [{"name": "Canon EOS R5", "count": 1}]
    assert body["lenses"] == []
    assert body["aperture_distribution"] == [{"value": "f/2.8", "count": 1}]
    assert body["iso_distribution"] == []
    assert body["focal_length_distribution"] == []
    assert body["shutter_speed_distribution"] == []


# ============================================================================
# EXIF — 3. 部分资产完全无 EXIF（模拟侧载）：不报错，warnings 非空
# ============================================================================


def test_exif_partial_no_exif_warnings():
    """部分资产无 exifInfo（侧载文件）：不报错，warnings 提示无 EXIF 数量。"""
    assets = [
        _asset(make="Canon", model="EOS R5"),
        _asset(),  # 侧载，无 EXIF
        _asset(),  # 侧载，无 EXIF
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["degraded"] is False
    assert len(body["warnings"]) == 1
    assert "2" in body["warnings"][0]
    assert "无 EXIF" in body["warnings"][0]


# ============================================================================
# EXIF — 4. 空数据 total=0，不报错
# ============================================================================


def test_exif_empty_data():
    """无任何资产：total=0，所有分布为空，不报错。"""
    immich = FakeImmichClient([])
    with make_client(immich=immich) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["degraded"] is False
    assert body["camera_brands"] == []
    assert body["cameras"] == []
    assert body["lenses"] == []
    assert body["aperture_distribution"] == []
    assert body["iso_distribution"] == []
    assert body["focal_length_distribution"] == []
    assert body["shutter_speed_distribution"] == []


# ============================================================================
# EXIF — 5. 上游不可用返回 degraded=true + warnings，状态码 200
# ============================================================================


def test_exif_upstream_failure_degraded():
    """Immich 上游不可用：降级返回 total=0 + degraded=true + warnings，状态码 200。"""
    immich = FailingImmichClient()
    with make_client(immich=immich) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["degraded"] is True
    assert len(body["warnings"]) == 1
    assert "上游" in body["warnings"][0]


# ============================================================================
# EXIF — 6. STATS_ENABLED=false 返回 503 + SERVICE_UNAVAILABLE
# ============================================================================


def test_exif_stats_disabled_503():
    """STATS_ENABLED=false：返回 503 + SERVICE_UNAVAILABLE。"""
    settings = _fake_settings(stats_enabled=False)
    immich = FakeImmichClient([])
    with make_client(immich=immich, settings=settings) as c:
        resp = _get_stats(c)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"


# ============================================================================
# EXIF — 7. 无 API Key 返回 401
# ============================================================================


def test_exif_no_api_key_401():
    """配置了 search_api_key 但请求未携带 x-api-key：返回 401。"""
    settings = _fake_settings(search_api_key="secret-key")
    immich = FakeImmichClient([])
    with make_client(immich=immich, settings=settings) as c:
        resp = _get_stats(c)

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_exif_with_api_key_200():
    """配置了 search_api_key 且请求携带正确 x-api-key：返回 200。"""
    settings = _fake_settings(search_api_key="secret-key")
    immich = FakeImmichClient([])
    with make_client(immich=immich, settings=settings) as c:
        resp = _get_stats(c, headers={"x-api-key": "secret-key"})

    assert resp.status_code == 200


# ============================================================================
# EXIF — 8. 缓存命中不重复拉取
# ============================================================================


def test_exif_cache_hit_no_refetch():
    """第二次请求命中缓存：不再次调用 Immich。"""
    assets = [_asset(make="Canon", model="EOS R5")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp1 = _get_stats(c)
        assert resp1.status_code == 200
        assert immich.call_count == 1

        resp2 = _get_stats(c)
        assert resp2.status_code == 200
        assert immich.call_count == 1  # 未再次拉取

        body1 = resp1.json()
        body2 = resp2.json()
        assert body1 == body2


# ============================================================================
# EXIF — 9. 前置模块（stats_provider）不存在时降级路径可用
# ============================================================================


def test_exif_stats_provider_unavailable_fallback(monkeypatch):
    """_HAS_STATS_PROVIDER=False 时使用 fallback 分页拉取，结果正确。"""
    monkeypatch.setattr("app.api.routes.stats._HAS_STATS_PROVIDER", False)

    assets = [
        _asset(make="Sony", model="A7R4", f_number=4.0, iso=800, focal_length=35, exposure_time=0.001),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["camera_brands"] == [{"name": "Sony", "count": 1}]
    assert immich.call_count >= 1  # fallback 确实调用了 Immich


# ============================================================================
# EXIF — 10. StatsProvider 可用时正常工作
# ============================================================================


def test_exif_stats_provider_available(monkeypatch):
    """_HAS_STATS_PROVIDER=True 且 StatsProvider 可用时：正常聚合。"""
    monkeypatch.setattr("app.api.routes.stats._HAS_STATS_PROVIDER", True)
    monkeypatch.setattr("app.api.routes.stats.StatsProvider", FakeStatsProvider)

    assets = [
        _asset(make="Fujifilm", model="X-T5", lens="XF 35mm", f_number=2.0, iso=200, focal_length=35, exposure_time=0.002),
        _asset(make="Fujifilm", model="X-T5", lens="XF 35mm", f_number=2.0, iso=400, focal_length=35, exposure_time=0.001),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["camera_brands"] == [{"name": "Fujifilm", "count": 2}]
    assert body["cameras"] == [{"name": "Fujifilm X-T5", "count": 2}]
    assert body["lenses"] == [{"name": "XF 35mm", "count": 2}]


# ============================================================================
# EXIF — 11. 不同日期参数使用不同缓存 key
# ============================================================================


def test_exif_date_params_cache_isolation():
    """不同 date_from/date_to 参数使用不同缓存 key，结果独立。"""
    assets = [_asset(make="Canon", model="EOS R5")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp1 = _get_stats(c, date_from="2024-01-01", date_to="2024-06-30")
        assert resp1.status_code == 200

        resp2 = _get_stats(c, date_from="2024-07-01", date_to="2024-12-31")
        assert resp2.status_code == 200

        # 两次不同参数应触发两次拉取
        assert immich.call_count == 2

        # 相同参数第二次命中缓存
        resp3 = _get_stats(c, date_from="2024-01-01", date_to="2024-06-30")
        assert resp3.status_code == 200
        assert immich.call_count == 2  # 未再次拉取


# ============================================================================
# EXIF — 12. 分页拉取多页数据
# ============================================================================


def test_exif_pagination_multi_page():
    """资产数超过 page_size 时正确分页拉取。"""
    assets = [
        _asset(make="Canon", model="EOS R5", iso=100 + i)
        for i in range(25)
    ]
    settings = _fake_settings(stats_page_size=10, stats_max_items=100)
    immich = FakeImmichClient(assets)
    with make_client(immich=immich, settings=settings) as c:
        resp = _get_stats(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 25
    assert immich.call_count == 3  # 10 + 10 + 5


# ============================================================================
# EXIF — 13. 降级结果不写入缓存
# ============================================================================


def test_exif_degraded_result_not_cached():
    """降级结果不写入缓存：下次上游恢复后重新拉取。"""
    cache = MemoryCache()

    # 第一次：上游不可用
    with make_client(immich=FailingImmichClient(), cache=cache) as c:
        resp1 = _get_stats(c)
    assert resp1.json()["degraded"] is True

    # 第二次：上游恢复，应重新拉取（缓存中无降级结果）
    assets = [_asset(make="Canon", model="EOS R5")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich, cache=cache) as c:
        resp2 = _get_stats(c)
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 1
    assert resp2.json()["degraded"] is False
