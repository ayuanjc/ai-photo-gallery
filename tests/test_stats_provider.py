"""统计数据 Provider 测试。

覆盖：正常分页、max_items 上限、时间过滤、上游异常降级、
非 200 状态降级、STATS_ENABLED=false 空返回、空结果、warnings 清理、
singleflight 并发去重。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.config import Settings
from app.services.stats_provider import StatsProvider


class FakeResponse:
    """模拟 httpx.Response。"""

    def __init__(self, status_code: int, data: list | dict) -> None:
        self.status_code = status_code
        self._data = data
        self.text = str(data)

    def json(self) -> list | dict:
        return self._data


class FakeImmichClient:
    """模拟 ImmichClient，按 page/size 参数返回资产切片。

    模拟真实 Immich 服务端行为：每个请求独立，按 page/size 返回对应切片。
    fail_on_call: 第几次调用（1-based）抛异常
    fail_status: 第几次调用（1-based）返回非 200 状态码
    """

    def __init__(
        self,
        items: list[dict],
        fail_on_call: int | None = None,
        fail_status: int | None = None,
    ) -> None:
        self._items = items
        self._fail_on_call = fail_on_call
        self._fail_status = fail_status
        self.call_count = 0

    async def _request_with_retry(self, method: str, path: str, **kwargs):
        self.call_count += 1
        if self._fail_on_call is not None and self.call_count == self._fail_on_call:
            raise RuntimeError("Immich down")
        if self._fail_status is not None and self.call_count == self._fail_status:
            return FakeResponse(503, {})
        body = kwargs.get("json", {})
        page = int(body.get("page", 1))
        size = int(body.get("size", 100))
        skip = (page - 1) * size
        return FakeResponse(200, self._items[skip:skip + size])

    @staticmethod
    def _parse_json(resp: FakeResponse) -> list | dict:
        return resp._data

    async def request_json(self, method: str, path: str, **kwargs):
        resp = await self._request_with_retry(method, path, **kwargs)
        if resp.status_code != 200:
            raise RuntimeError(f"Immich 资产拉取失败: status={resp.status_code}")
        return self._parse_json(resp)

    @staticmethod
    def extract_items(data):
        if isinstance(data, list):
            return data
        return []


def _settings(**kwargs) -> Settings:
    defaults = dict(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        stats_enabled=True,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _asset(asset_id: str, date: str | None = None) -> dict:
    """构造测试资产，date 传入 fileCreatedAt 和 exifInfo.dateTimeOriginal。"""
    dt = date or "2024-06-01T12:00:00+00:00"
    return {"id": asset_id, "fileCreatedAt": dt, "exifInfo": {"dateTimeOriginal": dt}}


def _items(n: int, prefix: str = "a") -> list[dict]:
    return [_asset(f"{prefix}{i}") for i in range(n)]


async def _collect(gen) -> list[dict]:
    return [item async for item in gen]


# ---- 正常分页 ----


async def test_normal_pagination_multi_page():
    """多页资产全部 yield。"""
    items = _items(250)
    fake = FakeImmichClient(items)
    provider = StatsProvider(_settings(), fake)

    result, _w = await provider.fetch_assets(page_size=100)

    assert len(result) == 250
    assert result[0]["id"] == "a0"
    assert result[100]["id"] == "a100"
    assert result[200]["id"] == "a200"
    assert fake.call_count <= 6  # 分批并发可能多发少量请求


async def test_single_page():
    """单页资产正常返回。"""
    fake = FakeImmichClient(_items(5))
    provider = StatsProvider(_settings(), fake)

    result, _w = await provider.fetch_assets(page_size=100)

    assert len(result) == 5


# ---- max_items 上限 ----


async def test_max_items_stops_iteration():
    """达到 max_items 停止拉取。"""
    fake = FakeImmichClient(_items(200))
    provider = StatsProvider(_settings(), fake)

    result, _w = await provider.fetch_assets(page_size=100, max_items=50)

    assert len(result) == 50
    assert result[49]["id"] == "a49"


async def test_max_items_exact_boundary():
    """max_items 恰好等于一页数量。"""
    fake = FakeImmichClient(_items(200))
    provider = StatsProvider(_settings(), fake)

    result, _w = await provider.fetch_assets(page_size=100, max_items=100)

    assert len(result) == 100


async def test_max_items_from_settings():
    """max_items 默认走 Settings.stats_max_items。"""
    fake = FakeImmichClient(_items(200))
    provider = StatsProvider(_settings(stats_max_items=10), fake)

    result, _w = await provider.fetch_assets(page_size=100)

    assert len(result) == 10


async def test_max_items_zero_returns_empty():
    """max_items=0 返回空。"""
    fake = FakeImmichClient(_items(2))
    provider = StatsProvider(_settings(), fake)

    result, _w = await provider.fetch_assets(page_size=100, max_items=0)

    assert result == []


# ---- 时间过滤 ----


async def test_date_from_filter():
    """只 yield date_from 之后的资产。"""
    items = [
        _asset("old1", "2023-01-01T00:00:00+00:00"),
        _asset("old2", "2023-06-01T00:00:00+00:00"),
        _asset("new1", "2024-01-01T00:00:00+00:00"),
        _asset("new2", "2024-06-01T00:00:00+00:00"),
    ]
    fake = FakeImmichClient(items)
    provider = StatsProvider(_settings(), fake)

    date_from = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result, _w = await provider.fetch_assets(page_size=100, date_from=date_from)

    ids = [item["id"] for item in result]
    assert "new1" in ids
    assert "new2" in ids
    assert "old1" not in ids
    assert "old2" not in ids


async def test_date_to_filter():
    """只 yield date_to 之前的资产。"""
    items = [
        _asset("old1", "2023-01-01T00:00:00+00:00"),
        _asset("old2", "2023-06-01T00:00:00+00:00"),
        _asset("new1", "2024-01-01T00:00:00+00:00"),
        _asset("new2", "2024-06-01T00:00:00+00:00"),
    ]
    fake = FakeImmichClient(items)
    provider = StatsProvider(_settings(), fake)

    date_to = datetime(2023, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    result, _w = await provider.fetch_assets(page_size=100, date_to=date_to)

    ids = [item["id"] for item in result]
    assert "old1" in ids
    assert "old2" in ids
    assert "new1" not in ids
    assert "new2" not in ids


async def test_date_range_filter():
    """date_from + date_to 区间过滤。"""
    items = [
        _asset("p1", "2023-01-01T00:00:00+00:00"),
        _asset("p2", "2023-06-01T00:00:00+00:00"),
        _asset("p3", "2024-01-01T00:00:00+00:00"),
        _asset("p4", "2024-06-01T00:00:00+00:00"),
        _asset("p5", "2025-01-01T00:00:00+00:00"),
    ]
    fake = FakeImmichClient(items)
    provider = StatsProvider(_settings(), fake)

    date_from = datetime(2023, 6, 1, tzinfo=timezone.utc)
    date_to = datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    result, _w = await provider.fetch_assets(
        page_size=100, date_from=date_from, date_to=date_to
    )

    ids = [item["id"] for item in result]
    assert ids == ["p2", "p3", "p4"]


async def test_asset_without_date_skipped_when_filtering():
    """无日期的资产在时间过滤时跳过。"""
    items = [
        {"id": "no_date", "fileCreatedAt": None},
        _asset("with_date", "2024-06-01T00:00:00+00:00"),
    ]
    fake = FakeImmichClient(items)
    provider = StatsProvider(_settings(), fake)

    date_from = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result, _w = await provider.fetch_assets(page_size=100, date_from=date_from)

    ids = [item["id"] for item in result]
    assert "with_date" in ids
    assert "no_date" not in ids


# ---- 上游异常降级 ----


async def test_upstream_exception_degrades_gracefully():
    """第二页抛异常，第一页已拉到的正常返回。"""
    fake = FakeImmichClient(_items(200), fail_on_call=2)
    provider = StatsProvider(_settings(), fake)

    result, warnings = await provider.fetch_assets(page_size=100)

    assert len(result) == 100
    assert all(item["id"].startswith("a") for item in result)
    assert len(warnings) == 1
    assert "Immich" in warnings[0]


async def test_upstream_exception_on_first_page():
    """第一页就抛异常，返回空，不抛。"""
    fake = FakeImmichClient(_items(100), fail_on_call=1)
    provider = StatsProvider(_settings(), fake)

    result, warnings = await provider.fetch_assets(page_size=100)

    assert result == []
    assert len(warnings) == 1


# ---- 非 200 状态降级 ----


async def test_non_200_status_degrades():
    """非 200 状态码停止迭代，已拉到的返回。"""
    fake = FakeImmichClient(_items(200), fail_status=2)
    provider = StatsProvider(_settings(), fake)

    result, warnings = await provider.fetch_assets(page_size=100)

    assert len(result) == 100
    assert len(warnings) == 1
    assert "status=503" in warnings[0]


# ---- STATS_ENABLED=false ----


async def test_stats_disabled_returns_empty():
    """STATS_ENABLED=false 不拉取，返回空。"""
    fake = FakeImmichClient(_items(2))
    provider = StatsProvider(_settings(stats_enabled=False), fake)

    result, _w = await provider.fetch_assets()

    assert result == []
    assert fake.call_count == 0


# ---- 空结果 ----


async def test_empty_result():
    """Immich 返回空列表，正常停止。"""
    fake = FakeImmichClient([])
    provider = StatsProvider(_settings(), fake)

    result, _w = await provider.fetch_assets(page_size=100)

    assert result == []


async def test_no_more_pages():
    """拉完所有页后正常停止。"""
    fake = FakeImmichClient(_items(150))
    provider = StatsProvider(_settings(), fake)

    result, _w = await provider.fetch_assets(page_size=100)

    assert len(result) == 150
    assert fake.call_count <= 6  # 分批并发可能多发少量请求


# ---- warnings 清理 ----


async def test_warnings_cleared_on_new_iteration():
    """每次新迭代前清空 warnings。"""
    fake = FakeImmichClient(_items(200), fail_on_call=2)
    provider = StatsProvider(_settings(), fake)

    _, warnings = await provider.fetch_assets(page_size=100)
    assert len(warnings) == 1

    fake2 = FakeImmichClient(_items(100))
    provider._immich = fake2
    _, warnings = await provider.fetch_assets(page_size=100)
    assert len(warnings) == 0


# ---- page_size 从 Settings ----


async def test_page_size_from_settings():
    """page_size 默认走 Settings.stats_page_size。"""
    fake = FakeImmichClient(_items(50))
    provider = StatsProvider(_settings(stats_page_size=50), fake)

    result, _w = await provider.fetch_assets()

    assert len(result) == 50


# ---- singleflight 并发去重 ----


async def test_singleflight_concurrent_same_params():
    """并发调用相同参数，只拉一次。"""
    fake = FakeImmichClient(_items(50))
    provider = StatsProvider(_settings(), fake)

    results = await asyncio.gather(
        provider.fetch_assets(page_size=100),
        provider.fetch_assets(page_size=100),
        provider.fetch_assets(page_size=100),
    )

    assert fake.call_count <= 5  # 分批并发
    for items, _w in results:
        assert len(items) == 50


async def test_singleflight_different_params_separate():
    """不同参数的并发调用各自独立拉取。"""
    fake = FakeImmichClient(_items(50))
    provider = StatsProvider(_settings(), fake)

    (r0, _), (r1, _) = await asyncio.gather(
        provider.fetch_assets(page_size=100, max_items=50),
        provider.fetch_assets(page_size=100, max_items=30),
    )

    assert fake.call_count <= 10  # 分批并发
    assert len(r0) == 50
    assert len(r1) == 30


async def test_singleflight_cleanup_after_completion():
    """任务完成后 inflight 字典清理。"""
    fake = FakeImmichClient(_items(50))
    provider = StatsProvider(_settings(), fake)

    await provider.fetch_assets(page_size=100)

    assert len(provider._inflight) == 0
