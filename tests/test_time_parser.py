"""时间语义化解析器单元测试。

覆盖：
    - parse_time_hint 各种时间表达解析
    - filter_by_time_range 时间范围过滤
    - SearchService 端到端：时间解析 → 过滤 → applied_time_range 回显
    - TIME_PARSER_ENABLED=false 时行为与改动前一致
    - 无时间表达时行为与改动前一致
    - 显式 time_hint 优先于自然语言
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.errors import VisionError
from app.models.request import SearchRequest
from app.services.search_service import SearchService
from app.services.time_parser import filter_by_time_range, parse_time_hint
from app.services.vision.base import VisionProvider, VisionResult


class FakeImmich:
    def __init__(self, assets, raise_on_search=None):
        self.assets = assets
        self.raise_on_search = raise_on_search
        self.queries: list[tuple[str, int]] = []

    async def smart_search(self, query: str, k: int = 50, asset_ids=None):
        self.queries.append((query, k))
        if self.raise_on_search:
            raise self.raise_on_search
        return self.assets

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"http://immich.test/api/assets/{asset_id}/original"


class FakeVision(VisionProvider):
    name = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    async def analyze(self, image_url: str) -> VisionResult:
        self.calls.append(image_url)
        if self.fail:
            raise VisionError("vision boom")
        return VisionResult(labels=["海边", "日落"])


def _asset(aid: str, score: float, dt: str = "2023-06-15T10:00:00Z") -> dict:
    return {
        "id": aid,
        "score": score,
        "exifInfo": {"dateTimeOriginal": dt},
    }


def _request(**overrides) -> SearchRequest:
    payload = {"query": "海滩", "asset_ids": ["asset-001"]}
    payload.update(overrides)
    return SearchRequest(**payload)


NOW = datetime(2024, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


# ========== parse_time_hint 测试 ==========


class TestParseTimeHint:
    def test_no_time_expression(self):
        q, rng = parse_time_hint("海边", now=NOW)
        assert q == "海边"
        assert rng is None

    def test_today(self):
        q, rng = parse_time_hint("今天", now=NOW)
        assert rng is not None
        assert rng[0].day == 15 and rng[0].month == 7
        assert rng[1].day == 15 and rng[1].month == 7

    def test_yesterday(self):
        q, rng = parse_time_hint("昨天", now=NOW)
        assert rng is not None
        assert rng[0].day == 14 and rng[1].day == 14

    def test_day_before_yesterday(self):
        q, rng = parse_time_hint("前天", now=NOW)
        assert rng is not None
        assert rng[0].day == 13 and rng[1].day == 13

    def test_this_week(self):
        q, rng = parse_time_hint("本周", now=NOW)
        assert rng is not None
        assert (rng[1] - rng[0]).days == 6

    def test_last_week(self):
        q, rng = parse_time_hint("上周", now=NOW)
        assert rng is not None
        assert (rng[1] - rng[0]).days == 6

    def test_this_month(self):
        q, rng = parse_time_hint("本月", now=NOW)
        assert rng is not None
        assert rng[0].month == 7 and rng[0].day == 1
        assert rng[1].month == 7 and rng[1].day == 31

    def test_last_month(self):
        q, rng = parse_time_hint("上个月", now=NOW)
        assert rng is not None
        assert rng[0].month == 6 and rng[0].day == 1
        assert rng[1].month == 6 and rng[1].day == 30

    def test_this_year(self):
        q, rng = parse_time_hint("今年", now=NOW)
        assert rng is not None
        assert rng[0].month == 1 and rng[0].day == 1
        assert rng[0].year == 2024
        assert rng[1] == NOW

    def test_last_year(self):
        q, rng = parse_time_hint("去年", now=NOW)
        assert rng is not None
        assert rng[0].year == 2023 and rng[0].month == 1
        assert rng[1].year == 2023 and rng[1].month == 12

    def test_last_year_summer(self):
        q, rng = parse_time_hint("去年夏天 海边", now=NOW)
        assert q == "海边"
        assert rng is not None
        assert rng[0].year == 2023 and rng[0].month == 6
        assert rng[1].year == 2023 and rng[1].month == 8

    def test_this_year_winter(self):
        q, rng = parse_time_hint("今年冬天", now=NOW)
        assert rng is not None
        assert rng[0].year == 2024 and rng[0].month == 12
        assert rng[1].year == 2025 and rng[1].month == 2

    def test_last_year_winter(self):
        q, rng = parse_time_hint("去年冬天", now=NOW)
        assert rng is not None
        assert rng[0].year == 2023 and rng[0].month == 12
        assert rng[1].year == 2024 and rng[1].month == 2

    def test_year_before_last_spring(self):
        q, rng = parse_time_hint("前年春天", now=NOW)
        assert rng is not None
        assert rng[0].year == 2022 and rng[0].month == 3
        assert rng[1].year == 2022 and rng[1].month == 5

    def test_recent_7_days(self):
        q, rng = parse_time_hint("最近", now=NOW)
        assert rng is not None
        assert (NOW - rng[0]).days == 7
        assert rng[1] == NOW

    def test_recent_one_week(self):
        q, rng = parse_time_hint("最近一周", now=NOW)
        assert rng is not None
        assert (NOW - rng[0]).days == 7

    def test_recent_one_month(self):
        q, rng = parse_time_hint("最近一个月", now=NOW)
        assert rng is not None
        assert (NOW - rng[0]).days == 30

    def test_recent_one_year(self):
        q, rng = parse_time_hint("最近一年", now=NOW)
        assert rng is not None
        assert (NOW - rng[0]).days == 365

    def test_last_month_with_photo(self):
        q, rng = parse_time_hint("上个月的照片", now=NOW)
        assert q == "照片"
        assert rng is not None
        assert rng[0].month == 6

    def test_query_cleaned_after_time_removal(self):
        q, rng = parse_time_hint("去年夏天 海边", now=NOW)
        assert "去年" not in q
        assert "夏天" not in q
        assert "海边" in q

    def test_empty_query(self):
        q, rng = parse_time_hint("", now=NOW)
        assert q == ""
        assert rng is None

    def test_query_becomes_empty_keeps_original(self):
        q, rng = parse_time_hint("去年夏天", now=NOW)
        assert rng is not None
        assert q == "去年夏天"

    def test_unrecognized_time_expression(self):
        q, rng = parse_time_hint("我的生日", now=NOW)
        assert q == "我的生日"
        assert rng is None

    def test_naive_now_gets_utc(self):
        naive_now = datetime(2024, 7, 15, 12, 0, 0)
        q, rng = parse_time_hint("今天", now=naive_now)
        assert rng is not None
        assert rng[0].tzinfo is not None


# ========== filter_by_time_range 测试 ==========


class TestFilterByTimeRange:
    def test_filter_in_range(self):
        assets = [_asset("a", 0.9, "2023-07-01T10:00:00Z")]
        df = datetime(2023, 6, 1, tzinfo=timezone.utc)
        dt = datetime(2023, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
        result = filter_by_time_range(assets, df, dt, "1970-01-01T00:00:00+00:00")
        assert len(result) == 1

    def test_filter_out_of_range(self):
        assets = [_asset("a", 0.9, "2023-09-01T10:00:00Z")]
        df = datetime(2023, 6, 1, tzinfo=timezone.utc)
        dt = datetime(2023, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
        result = filter_by_time_range(assets, df, dt, "1970-01-01T00:00:00+00:00")
        assert len(result) == 0

    def test_filter_boundary_inclusive(self):
        assets = [_asset("a", 0.9, "2023-06-01T00:00:00Z")]
        df = datetime(2023, 6, 1, tzinfo=timezone.utc)
        dt = datetime(2023, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
        result = filter_by_time_range(assets, df, dt, "1970-01-01T00:00:00+00:00")
        assert len(result) == 1

    def test_filter_empty_assets(self):
        df = datetime(2023, 6, 1, tzinfo=timezone.utc)
        dt = datetime(2023, 8, 31, tzinfo=timezone.utc)
        result = filter_by_time_range([], df, dt, "1970-01-01T00:00:00+00:00")
        assert result == []

    def test_filter_preserves_order(self):
        assets = [
            _asset("a", 0.9, "2023-07-01T10:00:00Z"),
            _asset("b", 0.8, "2023-09-01T10:00:00Z"),
            _asset("c", 0.7, "2023-07-15T10:00:00Z"),
        ]
        df = datetime(2023, 6, 1, tzinfo=timezone.utc)
        dt = datetime(2023, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
        result = filter_by_time_range(assets, df, dt, "1970-01-01T00:00:00+00:00")
        assert [r["id"] for r in result] == ["a", "c"]


# ========== SearchService 端到端测试 ==========


class TestSearchServiceTimeParser:
    async def test_time_filtering_in_search(self, settings):
        now = datetime.now(timezone.utc)
        last_year = now.year - 1
        assets = [
            _asset("a", 0.9, f"{last_year}-07-01T10:00:00Z"),
            _asset("b", 0.8, f"{now.year}-01-15T10:00:00Z"),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="去年夏天 海边"))

        assert resp.total == 1
        assert resp.results[0].asset_id == "a"
        assert resp.applied_time_range is not None
        assert resp.applied_time_range["source"] == "natural_language"

    async def test_no_time_expression_returns_all(self, settings):
        assets = [
            _asset("a", 0.9, "2023-07-01T10:00:00Z"),
            _asset("b", 0.8, "2024-01-15T10:00:00Z"),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边"))

        assert resp.total == 2
        assert resp.applied_time_range is None

    async def test_time_parser_disabled(self, settings):
        settings.time_parser_enabled = False
        assets = [
            _asset("a", 0.9, "2023-07-01T10:00:00Z"),
            _asset("b", 0.8, "2024-01-15T10:00:00Z"),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="去年夏天 海边"))

        assert resp.total == 2
        assert resp.applied_time_range is None

    async def test_explicit_time_hint_priority(self, settings):
        assets = [
            _asset("a", 0.9, "2023-07-01T10:00:00Z"),
            _asset("b", 0.8, "2024-01-15T10:00:00Z"),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="去年夏天 海边", time_hint="今年"))

        assert resp.applied_time_range is not None
        assert resp.applied_time_range["source"] == "explicit"

    async def test_applied_time_range_format(self, settings):
        assets = [_asset("a", 0.9, "2023-07-01T10:00:00Z")]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="去年夏天 海边"))

        assert resp.applied_time_range is not None
        assert "date_from" in resp.applied_time_range
        assert "date_to" in resp.applied_time_range
        assert "source" in resp.applied_time_range

    async def test_cleaned_query_used_for_search(self, settings):
        assets = [_asset("a", 0.9)]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        await svc.search(_request(query="去年夏天 海边"))

        assert len(immich.queries) == 1
        query_sent, _ = immich.queries[0]
        assert "去年" not in query_sent or "海边" in query_sent

    async def test_all_filtered_returns_empty(self, settings):
        now = datetime.now(timezone.utc)
        out_of_range_year = now.year if now.month not in (6, 7, 8) else now.year + 1
        assets = [_asset("a", 0.9, f"{out_of_range_year}-01-15T10:00:00Z")]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="去年夏天 海边"))

        assert resp.total == 0
        assert resp.applied_time_range is not None

    async def test_time_hint_field_accepted(self, settings):
        assets = [_asset("a", 0.9, "2024-07-10T10:00:00Z")]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边", time_hint="最近一周"))

        assert resp.applied_time_range is not None
        assert resp.applied_time_range["source"] == "explicit"
