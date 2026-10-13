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
from typing import TYPE_CHECKING, AsyncGenerator

from .immich import ImmichClient

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
    warnings 列表记录降级原因，调用方可在迭代结束后检查。
    并发安全：相同参数的并发请求通过 singleflight 共享同一个拉取任务。
    """

    def __init__(self, settings: Settings, immich: ImmichClient) -> None:
        self._settings = settings
        self._immich = immich
        self.warnings: list[str] = []
        self._inflight: dict[str, asyncio.Future[list[dict]]] = {}

    @staticmethod
    def _make_key(
        page_size: int,
        max_items: int,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> str:
        """根据参数生成 singleflight 缓存键。"""
        parts = [f"ps={page_size}", f"mi={max_items}"]
        if date_from:
            parts.append(f"df={date_from.isoformat()}")
        if date_to:
            parts.append(f"dt={date_to.isoformat()}")
        return "|".join(parts)

    async def iter_assets(
        self,
        page_size: int | None = None,
        max_items: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> AsyncGenerator[dict, None]:
        """异步迭代器：分页拉取 Immich 资产，逐个 yield。

        参数:
            page_size: 单页拉取数量（默认走 Settings.stats_page_size）
            max_items: 最多 yield 的资产数（默认走 Settings.stats_max_items）
            date_from: 只 yield 此时间之后的资产（含）
            date_to: 只 yield 此时间之前的资产（含）

        降级:
            - STATS_ENABLED=false → 不拉取，直接返回
            - 上游异常 → 记录日志 + warnings，停止迭代，不抛
            - page_size <= 0 → 钳制为 1
            - max_items <= 0 → 返回空

        并发安全:
            相同参数的并发请求通过 singleflight 共享同一个拉取任务，
            只向 Immich 发起一次分页拉取。
        """
        if not getattr(self._settings, "stats_enabled", True):
            return

        if page_size is None:
            page_size = getattr(self._settings, "stats_page_size", 100)
        if max_items is None:
            max_items = getattr(self._settings, "stats_max_items", 10000)
        page_size = max(1, page_size)
        max_items = max(0, max_items)

        if max_items == 0:
            return

        key = self._make_key(page_size, max_items, date_from, date_to)

        inflight = self._inflight.get(key)
        if inflight is not None and not inflight.done():
            items = await inflight
        else:
            self.warnings.clear()
            task = asyncio.create_task(
                self._fetch_all(page_size, max_items, date_from, date_to)
            )
            self._inflight[key] = task
            try:
                items = await task
            finally:
                self._inflight.pop(key, None)

        for item in items:
            yield item

    async def _fetch_all(
        self,
        page_size: int,
        max_items: int,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> list[dict]:
        """实际分页拉取逻辑，返回完整资产列表。

        上游异常时记录日志 + warnings，返回已拉到的部分。
        """
        results: list[dict] = []
        skip = 0

        while len(results) < max_items:
            try:
                resp = await self._immich._request_with_retry(
                    "GET",
                    "/api/assets",
                    params={"skip": skip, "take": page_size, "withExif": "true"},
                )
                if resp.status_code != 200:
                    msg = (
                        f"Immich 资产拉取失败: "
                        f"status={resp.status_code}, skip={skip}"
                    )
                    logger.warning(msg)
                    self.warnings.append(msg)
                    return results

                data = self._immich._parse_json(resp)
            except Exception as exc:
                msg = f"Immich 资产拉取异常，停止迭代: {exc}"
                logger.warning(msg)
                self.warnings.append(msg)
                return results

            items = ImmichClient._extract_items(data)

            if not items:
                return results

            for item in items:
                if len(results) >= max_items:
                    return results

                if date_from or date_to:
                    item_date = _get_asset_date(item)
                    if item_date is None:
                        continue
                    if date_from and item_date < date_from:
                        continue
                    if date_to and item_date > date_to:
                        continue

                results.append(item)

            skip += len(items)
            if len(items) < page_size:
                return results

        return results