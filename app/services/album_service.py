"""自动相册生成服务：按场景 + 时间聚类，自动生成"海边相册""聚会相册"等。

相册生成规则：
1. 按 primary_scene 分组（每个大场景一个相册候选）
2. 同一场景内，按拍摄时间排序，相邻照片时间差 > ALBUM_TIME_GAP_DAYS 则切分新相册
3. 每个相册取时间范围内第一张作封面
4. 相册数上限 ALBUM_MAX_COUNT

侧载资产支持：
- 无场景标签的资产不参与场景相册
- 无拍摄时间的资产不参与时间聚类
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..scene.analyzer import SceneAnalyzer
from .immich import ImmichClient
from .stats_provider import StatsProvider, _get_asset_date

if TYPE_CHECKING:
    from ..core.config import Settings

logger = logging.getLogger(__name__)

_SCENE_TITLES: dict[str, str] = {
    "nature": "自然风光",
    "city": "城市建筑",
    "people": "人物活动",
    "food": "美食饮品",
    "animal": "动物世界",
    "sport": "运动健身",
    "vehicle": "交通工具",
    "interior": "室内场景",
    "night": "夜景灯光",
    "abstract": "抽象艺术",
}


class AlbumService:
    """自动相册生成服务。"""

    def __init__(
        self,
        settings: Settings,
        immich: ImmichClient,
        scene_analyzer: SceneAnalyzer | None = None,
    ) -> None:
        self._settings = settings
        self._immich = immich
        self._scene_analyzer = scene_analyzer

    async def generate_albums(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[dict], list[str]]:
        """生成自动相册列表。

        参数:
            date_from: 起始时间（含），None 表示不限。
            date_to: 结束时间（含），None 表示不限。

        返回:
            (albums, warnings)
            albums: [{"album_id", "title", "scene", "cover_asset_id",
                      "asset_ids", "start_date", "end_date", "count"}, ...]
        """
        warnings: list[str] = []

        provider = StatsProvider(self._settings, self._immich)
        assets, fetch_warnings = await provider.fetch_assets(
            date_from=date_from, date_to=date_to
        )
        warnings.extend(fetch_warnings)

        if not assets:
            return [], warnings

        scene_groups = await self._group_by_scene(assets, warnings)

        time_gap_days = max(0, self._settings.album_time_gap_days)
        max_count = max(1, self._settings.album_max_count)

        albums: list[dict] = []
        for scene, scene_assets in scene_groups.items():
            clusters = self._cluster_by_time(scene_assets, time_gap_days)
            for cluster in clusters:
                if len(cluster) < 1:
                    continue
                album = self._build_album(scene, cluster)
                if album is not None:
                    albums.append(album)

        albums.sort(key=lambda a: a["start_date"], reverse=True)

        if len(albums) > max_count:
            albums = albums[:max_count]

        return albums, warnings

    async def _group_by_scene(
        self,
        assets: list[dict],
        warnings: list[str],
    ) -> dict[str, list[dict]]:
        """按 primary_scene 分组，无场景标签的资产跳过。"""
        groups: dict[str, list[dict]] = {}

        for asset in assets:
            scene = self._get_primary_scene(asset)
            if scene is None and self._scene_analyzer is not None:
                try:
                    tags = await self._scene_analyzer.analyze(asset)
                    scene = tags.primary_scene
                except Exception as exc:
                    logger.warning("场景分析失败, asset_id=%s: %s", asset.get("id"), exc)
                    continue

            if scene is None:
                continue

            date = _get_asset_date(asset)
            if date is None:
                continue

            groups.setdefault(scene, []).append(asset)

        return groups

    @staticmethod
    def _get_primary_scene(asset: dict) -> str | None:
        """从 asset 上提取 primary_scene（已附加的优先）。"""
        scene = asset.get("primary_scene")
        if scene and isinstance(scene, str):
            return scene
        scene_tags = asset.get("scene_tags")
        if scene_tags is not None:
            primary = getattr(scene_tags, "primary_scene", None)
            if primary and isinstance(primary, str):
                return primary
        return None

    @staticmethod
    def _cluster_by_time(
        assets: list[dict],
        gap_days: int,
    ) -> list[list[dict]]:
        """按时间间隔聚类，相邻照片时间差 > gap_days 则切分新相册。"""
        dated = [(a, _get_asset_date(a)) for a in assets]
        dated = [(a, d) for a, d in dated if d is not None]
        dated.sort(key=lambda x: x[1])

        if not dated:
            return []

        gap = timedelta(days=gap_days)
        clusters: list[list[dict]] = [[dated[0][0]]]
        prev_date = dated[0][1]

        for asset, date in dated[1:]:
            if date - prev_date > gap:
                clusters.append([asset])
            else:
                clusters[-1].append(asset)
            prev_date = date

        return clusters

    @staticmethod
    def _build_album(scene: str, cluster: list[dict]) -> dict | None:
        """从聚类簇构建单个相册。"""
        dated = [(a, _get_asset_date(a)) for a in cluster]
        dated = [(a, d) for a, d in dated if d is not None]
        if not dated:
            return None

        dated.sort(key=lambda x: x[1])
        cover = dated[0][0]
        start = dated[0][1]
        end = dated[-1][1]

        asset_ids = [str(a.get("id", "")) for a in cluster if a.get("id")]
        cover_id = str(cover.get("id", ""))
        if not cover_id:
            return None

        title = _SCENE_TITLES.get(scene, scene)
        date_suffix = start.strftime("%Y年%m月")
        album_title = f"{title} · {date_suffix}"

        return {
            "album_id": f"{scene}-{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}",
            "title": album_title,
            "scene": scene,
            "cover_asset_id": cover_id,
            "asset_ids": asset_ids,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(cluster),
        }
