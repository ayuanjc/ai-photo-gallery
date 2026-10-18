"""自动相册功能测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.services.album_service import AlbumService


def _settings(**overrides) -> Settings:
    defaults = dict(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        immich_timeout_seconds=1.0,
        immich_connect_timeout_seconds=1.0,
        immich_max_retries=0,
        immich_retry_backoff_seconds=0.0,
        time_weight=0.3,
        similarity_weight=0.7,
        time_decay_half_life_days=365.0,
        default_datetime_iso="1970-01-01T00:00:00+00:00",
        stats_enabled=True,
        stats_page_size=100,
        stats_max_items=10000,
        album_enabled=True,
        album_time_gap_days=3,
        album_max_count=100,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeImmich:
    """假 Immich 客户端，模拟分页拉取。

    与 ImmichClient 的公开接口保持一致：AlbumService 经 StatsProvider 调用
    request_json(POST /api/search/metadata) + extract_items，因此响应形状
    与真实后端一致（assets.items + total + nextPage），页码取自 json payload。
    """

    def __init__(self, assets: list[dict]) -> None:
        self._assets = assets

    @staticmethod
    def _extract_items(data):
        if isinstance(data, dict):
            items = (data.get("assets") or {}).get("items", [])
        else:
            items = data
        return [x for x in (items or []) if isinstance(x, dict)]

    @staticmethod
    def extract_items(data):
        return FakeImmich._extract_items(data)

    async def request_json(self, method, path, **kw):
        payload = kw.get("json") or {}
        page = int(payload.get("page", 1))
        size = int(payload.get("size", 100))
        start = (page - 1) * size
        return {
            "assets": {
                "items": self._assets[start : start + size],
                "total": len(self._assets),
                "nextPage": str(page + 1),
            }
        }


def _asset(
    aid: str,
    scene: str | None = None,
    date: datetime | None = None,
) -> dict:
    """构造测试资产。"""
    asset: dict = {"id": aid}
    if scene:
        asset["primary_scene"] = scene
    if date:
        asset["exifInfo"] = {"dateTimeOriginal": date.isoformat()}
    return asset


def _dt(days_ago: int) -> datetime:
    return datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc) - timedelta(days=days_ago)


# ============================================================================
# AlbumService 单元测试
# ============================================================================


class TestGenerateAlbums:
    async def test_normal_grouping(self):
        """同场景同时间段归为一个相册。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", "nature", _dt(1)),
            _asset("a3", "nature", _dt(2)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 1
        assert albums[0]["scene"] == "nature"
        assert albums[0]["count"] == 3
        assert albums[0]["cover_asset_id"] == "a3"  # 最早的照片
        assert set(albums[0]["asset_ids"]) == {"a1", "a2", "a3"}

    async def test_time_gap_splits_album(self):
        """时间间隔超阈值切分新相册。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", "nature", _dt(1)),
            _asset("a3", "nature", _dt(10)),
            _asset("a4", "nature", _dt(11)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(album_time_gap_days=3), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 2
        assert albums[0]["count"] == 2
        assert albums[1]["count"] == 2

    async def test_different_scenes_separate_albums(self):
        """不同场景归入不同相册。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", "city", _dt(0)),
            _asset("a3", "food", _dt(0)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 3
        scenes = {a["scene"] for a in albums}
        assert scenes == {"nature", "city", "food"}

    async def test_no_scene_asset_excluded(self):
        """无场景标签资产不参与场景相册。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", None, _dt(0)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 1
        assert albums[0]["count"] == 1
        assert albums[0]["asset_ids"] == ["a1"]

    async def test_no_time_asset_excluded(self):
        """无时间资产不参与时间聚类。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", "nature", None),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 1
        assert albums[0]["count"] == 1
        assert albums[0]["asset_ids"] == ["a1"]

    async def test_album_max_count_truncation(self):
        """相册数超上限截断。"""
        assets = []
        for i in range(20):
            scene = f"scene_{i}"
            assets.append(_asset(f"a{i}", scene, _dt(i)))
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(album_max_count=5), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 5

    async def test_empty_library(self):
        """空库返回 total_albums=0。"""
        immich = FakeImmich([])
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert albums == []
        assert warnings == []

    async def test_album_has_correct_fields(self):
        """相册包含所有必需字段。"""
        assets = [_asset("a1", "nature", _dt(0))]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 1
        album = albums[0]
        assert "album_id" in album
        assert "title" in album
        assert "scene" in album
        assert "cover_asset_id" in album
        assert "asset_ids" in album
        assert "start_date" in album
        assert "end_date" in album
        assert "count" in album

    async def test_album_title_contains_scene_name(self):
        """相册标题包含场景中文名。"""
        assets = [_asset("a1", "nature", _dt(0))]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 1
        assert "自然风光" in albums[0]["title"]

    async def test_albums_sorted_by_start_date_desc(self):
        """相册按起始时间降序排列。"""
        assets = [
            _asset("a1", "nature", _dt(30)),
            _asset("a2", "city", _dt(0)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 2
        assert albums[0]["start_date"] > albums[1]["start_date"]
        assert albums[0]["scene"] == "city"

    async def test_time_gap_zero(self):
        """ALBUM_TIME_GAP_DAYS=0 时每张照片独立成相册。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", "nature", _dt(1)),
            _asset("a3", "nature", _dt(2)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(album_time_gap_days=0), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 3

    async def test_partial_assets_no_scene(self):
        """部分资产有场景、部分无场景：只生成有场景的相册。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", "nature", _dt(1)),
            _asset("a3", None, _dt(0)),
            _asset("a4", None, _dt(1)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 1
        assert albums[0]["count"] == 2

    async def test_partial_assets_no_time(self):
        """部分资产有时间、部分无时间：只参与有时间相册。"""
        assets = [
            _asset("a1", "nature", _dt(0)),
            _asset("a2", "nature", _dt(1)),
            _asset("a3", "nature", None),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums()

        assert len(albums) == 1
        assert albums[0]["count"] == 2

    async def test_upstream_failure_degrades(self):
        """上游失败降级返回空 + warnings。"""
        class _FailImmich:
            async def _request_with_retry(self, method, path, **kw):
                raise RuntimeError("Immich down")

            @staticmethod
            def _parse_json(resp):
                return {}

            @staticmethod
            def _extract_items(data):
                return []

            async def request_json(self, method, path, **kw):
                raise RuntimeError("Immich down")

            @staticmethod
            def extract_items(data):
                return []

        svc = AlbumService(_settings(), _FailImmich())

        albums, warnings = await svc.generate_albums()

        assert albums == []
        assert len(warnings) >= 1

    async def test_date_filtering(self):
        """date_from/date_to 过滤资产范围。"""
        base = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets = [
            _asset("a1", "nature", base - timedelta(days=30)),
            _asset("a2", "nature", base),
            _asset("a3", "nature", base + timedelta(days=30)),
        ]
        immich = FakeImmich(assets)
        svc = AlbumService(_settings(), immich)

        albums, warnings = await svc.generate_albums(
            date_from=base - timedelta(days=5),
            date_to=base + timedelta(days=5),
        )

        assert len(albums) == 1
        assert albums[0]["count"] == 1
        assert albums[0]["asset_ids"] == ["a2"]


# ============================================================================
# 请求模型与响应模型测试
# ============================================================================


class TestAlbumModels:
    def test_album_model_valid(self):
        from app.models.response import Album

        album = Album(
            album_id="nature-20240615-20240615",
            title="自然风光 · 2024年06月",
            scene="nature",
            cover_asset_id="a1",
            asset_ids=["a1", "a2"],
            start_date="2024-06-15T10:00:00+00:00",
            end_date="2024-06-15T10:00:00+00:00",
            count=2,
        )
        assert album.count == 2
        assert album.scene == "nature"

    def test_album_list_response_valid(self):
        from app.models.response import AlbumListResponse

        resp = AlbumListResponse(
            total_albums=0,
            degraded=False,
            warnings=[],
            albums=[],
        )
        assert resp.total_albums == 0
        assert resp.cached is False

    def test_album_list_response_with_albums(self):
        from app.models.response import Album, AlbumListResponse

        album = Album(
            album_id="test-1",
            title="测试相册",
            scene="nature",
            cover_asset_id="a1",
            asset_ids=["a1"],
            start_date="2024-06-15T10:00:00+00:00",
            end_date="2024-06-15T10:00:00+00:00",
            count=1,
        )
        resp = AlbumListResponse(
            total_albums=1,
            albums=[album],
        )
        assert resp.total_albums == 1
        assert len(resp.albums) == 1
