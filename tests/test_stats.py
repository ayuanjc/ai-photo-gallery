"""器材偏好统计接口测试。

覆盖：ratio 计算、favorite_combo、shot_by_month 近 12 个月、无器材信息资产、
无拍摄时间资产、空数据防除零、上游降级、STATS_ENABLED=false 503、鉴权、
StatsProvider 降级、缓存命中。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
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
    make: str | None = None,
    model: str | None = None,
    lens: str | None = None,
    date_time: str | None = None,
) -> dict:
    """构造测试资产（含器材/拍摄时间信息）。"""
    exif: dict[str, Any] = {}
    if make is not None:
        exif["make"] = make
    if model is not None:
        exif["model"] = model
    if lens is not None:
        exif["lensModel"] = lens
    if date_time is not None:
        exif["dateTimeOriginal"] = date_time
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


def _get_gear(client: TestClient, headers: dict | None = None, **params: Any):
    return client.get("/api/v1/stats/gear", params=params, headers=headers)


# ============================================================================
# 1. ratio 计算正确
# ============================================================================


def test_ratio_and_aggregation():
    """ratio = count/total 保留 2 位小数，top/brand 聚合正确。"""
    assets = [
        _asset("1", make="Canon", model="EOS R5", lens="RF 24-70"),
        _asset("2", make="Canon", model="EOS R5", lens="RF 24-70"),
        _asset("3", make="Nikon", model="Z7", lens="Z 50mm"),
        _asset("4", make="Sony", model="A7R4", lens="FE 35mm"),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4

    cams = {x["name"]: x for x in body["top_cameras"]}
    assert cams["Canon EOS R5"]["count"] == 2
    assert cams["Canon EOS R5"]["ratio"] == 0.5
    assert cams["Nikon Z7"]["ratio"] == 0.25
    assert cams["Sony A7R4"]["ratio"] == 0.25

    lenses = {x["name"]: x for x in body["top_lenses"]}
    assert lenses["RF 24-70"]["count"] == 2
    assert lenses["RF 24-70"]["ratio"] == 0.5

    brands = {x["brand"]: x for x in body["brand_ratio"]}
    assert brands["Canon"]["count"] == 2
    assert brands["Canon"]["ratio"] == 0.5
    assert brands["Nikon"]["ratio"] == 0.25
    assert brands["Sony"]["ratio"] == 0.25


# ============================================================================
# 2. favorite_combo 正确
# ============================================================================


def test_favorite_combo():
    """最常用相机+镜头组合正确。"""
    assets = [
        _asset("1", make="Canon", model="EOS R5", lens="RF 24-70"),
        _asset("2", make="Canon", model="EOS R5", lens="RF 24-70"),
        _asset("3", make="Canon", model="EOS R5", lens="RF 70-200"),
        _asset("4", make="Nikon", model="Z7", lens="Z 50mm"),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    body = resp.json()
    combo = body["favorite_combo"]
    assert combo is not None
    assert combo["camera"] == "Canon EOS R5"
    assert combo["lens"] == "RF 24-70"
    assert combo["count"] == 2


def test_favorite_combo_none_when_no_pair():
    """无相机+镜头成对时 favorite_combo 为 null。"""
    assets = [
        _asset("1", make="Canon", model="EOS R5"),  # 只有相机无镜头
        _asset("2", lens="RF 24-70"),               # 只有镜头无相机
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    body = resp.json()
    assert body["favorite_combo"] is None


# ============================================================================
# 3. shot_by_month 只返近 12 个月
# ============================================================================


def test_shot_by_month_recent_12_months():
    """直接调用聚合函数（固定 now）：只返最近 12 个月，升序。"""
    from app.api.routes.stats import _aggregate_gear

    assets = [
        _asset("1", make="Canon", model="EOS R5", date_time="2026-10-05T10:00:00Z"),  # 当前月
        _asset("2", make="Canon", model="EOS R5", date_time="2025-11-05T10:00:00Z"),  # 12 个月前边界
        _asset("3", make="Nikon", model="Z7", date_time="2025-10-05T10:00:00Z"),      # 13 个月前，过滤
        _asset("4", make="Sony", model="A7R4", date_time="2024-01-05T10:00:00Z"),     # 很久前，过滤
    ]
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    warnings: list[str] = []
    agg = _aggregate_gear(assets, warnings, now=now)

    months = [m.month for m in agg["shot_by_month"]]
    assert months == ["2025-11", "2026-10"]


def test_shot_by_month_only_exif_time():
    """shot_by_month 只统计有拍摄时间的资产。"""
    assets = [
        _asset("1", make="Canon", model="EOS R5", date_time="2026-10-05T10:00:00Z"),
        _asset("2", make="Canon", model="EOS R5"),  # 无拍摄时间
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    body = resp.json()
    assert body["total"] == 2
    months = {m["month"]: m["count"] for m in body["shot_by_month"]}
    # 只有资产 1 的月份计入
    assert sum(months.values()) == 1


def test_shot_by_month_exif_format_handling():
    """兼容 EXIF 'YYYY:MM:DD' 与 ISO 'YYYY-MM-DD' 两种格式。"""
    from app.api.routes.stats import _aggregate_gear

    assets = [
        _asset("1", make="Canon", model="EOS R5", date_time="2026:10:05 10:00:00"),
        _asset("2", make="Nikon", model="Z7", date_time="2026-10-06T10:00:00Z"),
    ]
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    warnings: list[str] = []
    agg = _aggregate_gear(assets, warnings, now=now)

    months = {m.month: m.count for m in agg["shot_by_month"]}
    assert months == {"2026-10": 2}


# ============================================================================
# 4. 部分资产无器材信息：不计入 top，total 正常
# ============================================================================


def test_no_gear_not_in_top_total_normal():
    """无器材信息资产计入 total 但不在 top 系列。"""
    assets = [
        _asset("1", make="Canon", model="EOS R5", lens="RF 24-70"),
        _asset("2"),  # 无 exifInfo
        _asset("3", date_time="2026-10-05T10:00:00Z"),  # 有拍摄时间但无器材
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    body = resp.json()
    assert body["total"] == 3
    assert len(body["top_cameras"]) == 1
    assert body["top_cameras"][0]["name"] == "Canon EOS R5"
    assert body["top_cameras"][0]["ratio"] == 0.33  # 1/3 保留两位
    assert len(body["top_lenses"]) == 1
    # 缺器材信息记 warnings
    assert len(body["warnings"]) == 1
    assert "2" in body["warnings"][0]


# ============================================================================
# 5. 部分资产无拍摄时间：不计入 shot_by_month
# ============================================================================


def test_no_date_not_in_month():
    """无拍摄时间资产不计入 shot_by_month。"""
    from app.api.routes.stats import _aggregate_gear

    assets = [
        _asset("1", make="Canon", model="EOS R5", date_time="2026-10-05T10:00:00Z"),
        _asset("2", make="Nikon", model="Z7"),  # 无拍摄时间
        _asset("3"),  # 无任何信息
    ]
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    warnings: list[str] = []
    agg = _aggregate_gear(assets, warnings, now=now)

    months = {m.month: m.count for m in agg["shot_by_month"]}
    assert months == {"2026-10": 1}
    assert agg["total"] == 3


# ============================================================================
# 6. 空数据 total=0 不除零
# ============================================================================


def test_empty_data_no_divide_by_zero():
    """空数据 total=0：ratio 不除零，favorite_combo 为 null。"""
    immich = FakeImmichClient([])
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["degraded"] is False
    assert body["top_cameras"] == []
    assert body["top_lenses"] == []
    assert body["brand_ratio"] == []
    assert body["favorite_combo"] is None
    assert body["shot_by_month"] == []


# ============================================================================
# 7. 上游不可用返回 degraded=true + warnings
# ============================================================================


def test_upstream_failure_degraded():
    """Immich 上游不可用：降级返回 total=0 + degraded=true。"""
    immich = FailingImmichClient()
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
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
        resp = _get_gear(c)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ============================================================================
# 鉴权
# ============================================================================


def test_no_api_key_401():
    """配置 search_api_key 但未携带 x-api-key：返回 401。"""
    settings = _fake_settings(search_api_key="secret-key")
    with make_client(settings=settings) as c:
        resp = _get_gear(c)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_with_api_key_200():
    """携带正确 x-api-key：返回 200。"""
    settings = _fake_settings(search_api_key="secret-key")
    with make_client(settings=settings, immich=FakeImmichClient([])) as c:
        resp = _get_gear(c, headers={"x-api-key": "secret-key"})

    assert resp.status_code == 200


# ============================================================================
# 9. StatsProvider 降级路径
# ============================================================================


def test_stats_provider_unavailable_fallback(monkeypatch):
    """_HAS_STATS_PROVIDER=False 时使用 fallback 分页拉取。"""
    monkeypatch.setattr("app.api.routes.stats._HAS_STATS_PROVIDER", False)

    assets = [_asset("1", make="Canon", model="EOS R5", lens="RF 24-70")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["top_cameras"][0]["name"] == "Canon EOS R5"
    assert immich.call_count >= 1


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
        _asset("1", make="Canon", model="EOS R5", lens="RF 24-70"),
        _asset("2", make="Nikon", model="Z7", lens="Z 50mm"),
    ]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp = _get_gear(c)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["top_cameras"]) == 2


# ============================================================================
# 缓存
# ============================================================================


def test_cache_hit_no_refetch():
    """第二次相同参数请求命中缓存：不再次调用 Immich。"""
    assets = [_asset("1", make="Canon", model="EOS R5")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp1 = _get_gear(c)
        assert resp1.status_code == 200
        assert immich.call_count == 1

        resp2 = _get_gear(c)
        assert resp2.status_code == 200
        assert immich.call_count == 1


def test_different_date_cache_isolation():
    """不同日期参数使用不同缓存 key，分别拉取。"""
    assets = [_asset("1", make="Canon", model="EOS R5")]
    immich = FakeImmichClient(assets)
    with make_client(immich=immich) as c:
        resp1 = _get_gear(c, date_from="2024-01-01")
        resp2 = _get_gear(c, date_from="2025-01-01")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert immich.call_count == 2