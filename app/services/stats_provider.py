"""统计数据 Provider 基础层。

封装分页拉取 Immich 资产的异步迭代器，供后续三个统计模块共享。
只做数据获取，不做统计、不做聚合。

降级策略：上游失败时记录日志并停止迭代，已拉到的部分正常返回。
并发安全：相同参数的并发请求通过 singleflight 共享同一个拉取任务。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from .immich import ImmichClient
from ..core.runtime_config import runtime_enabled

if TYPE_CHECKING:
    from ..core.config import Settings

logger = logging.getLogger(__name__)


def _get_asset_date(asset: dict) -> datetime | None:
    """提取资产日期：优先 EXIF dateTimeOriginal，其次 fileCreatedAt。"""
    exif = asset.get("exifInfo") or {}
    date_str = exif.get("dateTimeOriginal") or asset.get("fileCreatedAt")
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class StatsProvider:
    """统计数据 Provider：分页拉取 Immich 资产 + EXIF。

    只做数据获取，不做统计。上游失败时降级返回已拉到的部分。
    并发安全：相同参数的并发请求通过 singleflight 共享同一个拉取任务（类级 _inflight）。
    """

    _inflight: dict[str, asyncio.Future[tuple[list[dict], list[str]]]] = {}

    def __init__(self, settings: Settings, immich: ImmichClient) -> None:
        self._settings = settings
        self._immich = immich

    @staticmethod
    def _make_key(
        page_size: int,
        max_items: int,
        date_from: datetime | str | None,
        date_to: datetime | str | None,
    ) -> str:
        """根据参数生成 singleflight 缓存键。"""
        parts = [f"ps={page_size}", f"mi={max_items}"]
        if date_from:
            parts.append(f"df={date_from.isoformat() if hasattr(date_from, 'isoformat') else date_from}")
        if date_to:
            parts.append(f"dt={date_to.isoformat() if hasattr(date_to, 'isoformat') else date_to}")
        return "|".join(parts)

    async def fetch_assets(
        self,
        page_size: int | None = None,
        max_items: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[dict], list[str]]:
        """分页拉取 Immich 资产，返回 (资产列表, 警告列表)。

        参数:
            page_size: 单页拉取数量（默认走 Settings.stats_page_size）
            max_items: 最多拉取的资产数（默认走 Settings.stats_max_items）
            date_from: 只拉取此时间之后的资产（含）
            date_to: 只拉取此时间之前的资产（含）

        降级:
            - STATS_ENABLED=false → 返回空列表
            - 上游异常 → 记录日志 + warnings，返回已拉到的部分

        并发安全:
            相同参数的并发请求通过 singleflight 共享同一个拉取任务，
            只向 Immich 发起一次分页拉取。
        """
        if not runtime_enabled(
            "stats_enabled", getattr(self._settings, "stats_enabled", True)
        ):
            return [], []

        if page_size is None:
            page_size = getattr(self._settings, "stats_page_size", 100)
        if max_items is None:
            max_items = getattr(self._settings, "stats_max_items", 10000)
        page_size = max(1, page_size)
        max_items = max(0, max_items)

        if max_items == 0:
            return [], []

        key = self._make_key(page_size, max_items, date_from, date_to)

        inflight = self._inflight.get(key)
        if inflight is not None and not inflight.done():
            items, warnings = await inflight
        else:
            task = asyncio.create_task(
                self._fetch_all(page_size, max_items, date_from, date_to)
            )
            self._inflight[key] = task
            try:
                items, warnings = await task
            finally:
                self._inflight.pop(key, None)

        return items, warnings

    async def _fetch_all(
        self,
        page_size: int,
        max_items: int,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> tuple[list[dict], list[str]]:
        """实际分页拉取逻辑，返回 (资产列表, 警告列表)。

        上游异常时记录日志 + warnings，返回已拉到的部分。
        """
        results: list[dict] = []
        warnings: list[str] = []
        page = 1

        while len(results) < max_items:
            try:
                resp = await self._immich._request_with_retry(
                    "POST",
                    "/api/search/metadata",
                    json={"page": page, "size": page_size, "withExif": True},
                )
                if resp.status_code != 200:
                    msg = (
                        f"Immich 资产拉取失败: "
                        f"status={resp.status_code}, page={page}"
                    )
                    logger.warning(msg)
                    warnings.append(msg)
                    return results, warnings

                data = self._immich._parse_json(resp)
            except Exception as exc:
                msg = f"Immich 资产拉取异常，停止迭代: {exc}"
                logger.warning(msg)
                warnings.append(msg)
                return results, warnings

            items = ImmichClient._extract_items(data)

            if not items:
                return results, warnings

            for item in items:
                if len(results) >= max_items:
                    return results, warnings

                if date_from or date_to:
                    item_date = _get_asset_date(item)
                    if item_date is None:
                        continue
                    if date_from and item_date < date_from:
                        continue
                    if date_to and item_date > date_to:
                        continue

                results.append(item)

            page += 1
            if len(items) < page_size:
                return results, warnings

        return results, warnings