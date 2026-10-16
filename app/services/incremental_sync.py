"""增量水位同步服务：按 updatedAt 水线增量拉取 Immich 资产变更。

流程：
1. 读取上次同步水位（updatedAt 截止时间）
2. 调用 POST /api/search/metadata，filter.updatedAt.start = 水位
3. 分页拉取（page_size=100，间隔 200ms 避免打满上游）
4. 对每批资产执行回调（如场景分析 + 写回）
5. 更新水位为最后一条资产的 updatedAt

水位持久化：JSON 文件 data/sync_watermark.json
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..core.config import Settings
from .immich import ImmichClient

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 100
_PAGE_INTERVAL_MS = 200
_DATA_DIR = Path("data")
_WATERMARK_PATH = _DATA_DIR / "sync_watermark.json"


SyncCallback = Callable[[list[dict[str, Any]]], Awaitable[None]]


class IncrementalSync:
    def __init__(
        self,
        immich: ImmichClient,
        settings: Settings,
        watermark_path: Path | None = None,
    ) -> None:
        self._immich = immich
        self._settings = settings
        self._watermark_path = watermark_path or _WATERMARK_PATH

    def _load_watermark(self) -> str | None:
        try:
            if self._watermark_path.exists():
                data = json.loads(self._watermark_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data.get("updatedAt")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("水位加载失败: %s", exc)
        return None

    def _save_watermark(self, updated_at: str) -> None:
        try:
            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
            self._watermark_path.write_text(
                json.dumps({"updatedAt": updated_at}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("水位保存失败: %s", exc)

    async def sync(
        self,
        callback: SyncCallback | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_pages: int = 100,
    ) -> dict[str, Any]:
        watermark = self._load_watermark()
        logger.info("增量同步开始, 水位=%s", watermark or "无（全量）")

        total_fetched = 0
        latest_updated: str | None = watermark
        page = 1

        while page <= max_pages:
            assets, has_more = await self._fetch_page(
                watermark, page, page_size
            )

            if not assets:
                break

            total_fetched += len(assets)

            page_latest = self._latest_updated_at(assets)
            if page_latest and (latest_updated is None or page_latest > latest_updated):
                latest_updated = page_latest

            if callback is not None:
                try:
                    await callback(assets)
                except Exception as exc:
                    logger.warning("同步回调执行失败: %s", exc)

            if not has_more:
                break

            page += 1
            await asyncio.sleep(_PAGE_INTERVAL_MS / 1000.0)

        if latest_updated and latest_updated != watermark:
            self._save_watermark(latest_updated)

        result = {
            "total_fetched": total_fetched,
            "pages": page,
            "watermark_before": watermark,
            "watermark_after": latest_updated,
        }
        logger.info("增量同步完成: %s", result)
        return result

    async def _fetch_page(
        self,
        watermark: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        payload: dict[str, Any] = {
            "searchTerm": "",
            "page": page,
            "size": page_size,
            "withExif": False,
        }
        if watermark:
            payload["filter"] = {
                "updatedAt": {"start": watermark}
            }

        try:
            data = await self._immich.request_json(
                "POST", "/api/search/metadata", json=payload
            )
        except Exception as exc:
            logger.error("增量同步拉取失败 page=%d: %s", page, exc)
            return [], False

        items = ImmichClient.extract_items(data)
        total = 0
        if isinstance(data, dict):
            assets_obj = data.get("assets", {})
            if isinstance(assets_obj, dict):
                total = assets_obj.get("total", 0)
            elif isinstance(assets_obj, list):
                total = len(assets_obj)

        has_more = (page * page_size) < total
        return items, has_more

    def _latest_updated_at(self, assets: list[dict[str, Any]]) -> str | None:
        latest: str | None = None
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            updated = asset.get("updatedAt")
            if updated and (latest is None or updated > latest):
                latest = updated
        return latest

    def reset_watermark(self) -> None:
        try:
            if self._watermark_path.exists():
                self._watermark_path.unlink()
        except OSError as exc:
            logger.warning("水位重置失败: %s", exc)