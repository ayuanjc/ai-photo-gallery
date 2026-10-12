"""地图足迹统计接口测试。

覆盖：有 GPS 聚合、cluster=city/raw 区分、show_ids 隐私控制、无 GPS 资产、
GPS 格式异常、上游降级、STATS_ENABLED=false 503、鉴权、StatsProvider 降级、
缓存命中、非法 cluster 回退。
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

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

    def __init__(self, all_assets: list[dict]) -> None:
        self._all_assets = all_assets
        self.call_count = 0

    async def _request_with_retry(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
        self.call_count += 1
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
) -> dict:
    """构造测试资产（含 GPS/地点信息）。"""
    exif: dict[str, Any] = {}
    if lat is not None:
        exif["latitude"] = lat
    if lng is not None:
        exif["longitude"] = lng
    if city is not None:
        exif["city"] = city
    if country is not None:
        exif["country"] = country
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


# ============================================================================
# 1. 有 GPS 资产聚合正确（cluster=city）
# ============================================================================


def test_normal_city_aggregation():
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
# 2. cluster=city 与 raw 行为区分
# ============================================================================


def test_cluster_city_returns_cities_not_points():
    """cluster=city：返回 cities，points 为空。"""
    assets = [_asset("1", lat=39.9, lng=116.4, city="北京", country="中国")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="city")

    body = resp.json()
    assert len(body["cities"]) == 1
    assert body["points"] == []
    assert len(body["countries"]) == 1


def test_cluster_raw_returns_points_not_cities():
    """cluster=raw：返回 points，cities 为空。"""
    assets = [_asset("1", lat=39.9, lng=116.4, city="北京", country="中国")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw")

    body = resp.json()
    assert len(body["points"]) == 1
    assert body["cities"] == []
    assert len(body["countries"]) == 1


def test_cluster_raw_merges_same_coordinate():
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
# 3. 默认不返回 asset_ids；4. show_ids=true 返回
# ============================================================================


def test_default_no_asset_ids():
    """show_ids 默认 false：points 的 asset_ids 为 null。"""
    assets = [_asset("1", lat=39.9, lng=116.4)]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="raw")

    body = resp.json()
    assert len(body["points"]) == 1
    assert body["points"][0]["asset_ids"] is None


def test_show_ids_true_returns_asset_ids():
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
# 5. 无 GPS 资产不计入
# ============================================================================


def test_no_gps_assets_not_counted():
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
# 6. GPS 格式异常：跳过并记 warnings
# ============================================================================


def test_gps_out_of_range_warnings():
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


def test_gps_half_missing_warnings():
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


def test_gps_non_numeric_warnings():
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
# 7. 上游不可用返回 degraded=true + warnings
# ============================================================================


def test_upstream_failure_degraded():
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
# 8. STATS_ENABLED=false 返回 503
# ============================================================================


def test_stats_disabled_503():
    """STATS_ENABLED=false：返回 503 + SERVICE_UNAVAILABLE。"""
    settings = _fake_settings(stats_enabled=False)
    with make_client(settings=settings) as c:
        resp = _get_map(c)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ============================================================================
# 鉴权
# ============================================================================


def test_no_api_key_401():
    """配置 search_api_key 但未携带 x-api-key：返回 401。"""
    settings = _fake_settings(search_api_key="secret-key")
    with make_client(settings=settings) as c:
        resp = _get_map(c)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_with_api_key_200():
    """携带正确 x-api-key：返回 200。"""
    settings = _fake_settings(search_api_key="secret-key")
    with make_client(settings=settings, immich=FakeImmichClient([])) as c:
        resp = _get_map(c, headers={"x-api-key": "secret-key"})

    assert resp.status_code == 200


# ============================================================================
# 9. StatsProvider 降级路径
# ============================================================================


def test_stats_provider_unavailable_fallback(monkeypatch):
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


def test_stats_provider_available(monkeypatch):
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
# 缓存
# ============================================================================


def test_cache_hit_no_refetch():
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


def test_different_cluster_cache_isolation():
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
# 非法 cluster 回退
# ============================================================================


def test_invalid_cluster_falls_back_to_city():
    """非法 cluster 值回退为 city，不报错。"""
    assets = [_asset("1", lat=39.9, lng=116.4, city="北京", country="中国")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_map(c, cluster="foobar")

    body = resp.json()
    assert resp.status_code == 200
    assert len(body["cities"]) == 1
    assert body["points"] == []