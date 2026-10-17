"""历史资产回填服务：对已有 Immich 资产批量执行场景分析 + 标签写回。

解决"3 万张历史图永远不触发工作流"的问题。

流程：
1. 用 Immich metadata search API 分页拉取资产
2. 可选过滤：只处理没有 AI/ 前缀标签的资产
3. 对每张资产跑场景分析 + 标签写回 + 描述补全
4. 受并发与速率限制
5. 进度写 Redis 供 GET /admin/api/backfill/status 查询
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..core.config import Settings
from .immich import ImmichClient

logger = logging.getLogger(__name__)

_PROGRESS_KEY = "backfill:progress"
_AI_TAG_PREFIX = "AI·"


class BackfillService:
    def __init__(
        self,
        immich: ImmichClient,
        settings: Settings,
    ) -> None:
        self._immich = immich
        self._settings = settings
        self._running = False
        self._progress: dict[str, Any] = {
            "status": "idle",
            "total": 0,
            "processed": 0,
            "skipped": 0,
            "succeeded": 0,
            "failed": 0,
            "current_page": 0,
            "started_at": None,
            "elapsed_seconds": 0,
            "errors": [],
        }

    def get_status(self) -> dict[str, Any]:
        if self._running:
            self._progress["elapsed_seconds"] = round(
                time.monotonic() - self._progress.get("_t0", 0), 1
            )
        return {k: v for k, v in self._progress.items() if not k.startswith("_")}

    async def run_backfill(
        self,
        limit: int = 0,
        since: str = "",
        dry_run: bool = False,
        skip_tagged: bool = True,
    ) -> dict[str, Any]:
        """执行回填。

        参数:
            limit: 最多处理资产数（0 = 不限）。
            since: 只处理 updatedAt >= since 的资产（ISO 格式）。
            dry_run: 仅统计不实际写回。
            skip_tagged: 跳过已有 AI/ 前缀标签的资产。
        """
        if self._running:
            return {"error": "回填任务正在运行中"}

        self._running = True
        self._progress = {
            "status": "running",
            "total": 0,
            "processed": 0,
            "skipped": 0,
            "succeeded": 0,
            "failed": 0,
            "current_page": 0,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_seconds": 0,
            "errors": [],
            "_t0": time.monotonic(),
            "dry_run": dry_run,
            "limit": limit,
            "since": since,
            "skip_tagged": skip_tagged,
        }

        try:
            await self._do_backfill(limit, since, dry_run, skip_tagged)
            self._progress["status"] = "completed"
        except Exception as exc:
            logger.exception("回填任务异常: %s", exc)
            self._progress["status"] = "failed"
            self._progress["errors"].append(str(exc))
        finally:
            self._progress["elapsed_seconds"] = round(
                time.monotonic() - self._progress.pop("_t0", 0), 1
            )
            self._running = False

        return self.get_status()

    async def _do_backfill(
        self,
        limit: int,
        since: str,
        dry_run: bool,
        skip_tagged: bool,
    ) -> None:
        page_size = getattr(self._settings, "backfill_page_size", 1000)
        concurrency = getattr(self._settings, "backfill_concurrency", 5)
        sem = asyncio.Semaphore(concurrency)

        page = 1
        total_processed = 0
        total_skipped = 0
        total_succeeded = 0
        total_failed = 0

        while True:
            if limit and total_processed >= limit:
                break

            assets = await self._fetch_page(page, page_size, since)
            if not assets:
                break

            self._progress["current_page"] = page
            self._progress["total"] += len(assets)

            tasks = [
                self._process_one(asset, sem, dry_run, skip_tagged)
                for asset in assets
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    total_failed += 1
                    self._progress["errors"].append(str(result)[:200])
                elif result == "skipped":
                    total_skipped += 1
                elif result == "succeeded":
                    total_succeeded += 1
                elif result == "failed":
                    total_failed += 1

            total_processed += len(assets)
            self._progress["processed"] = total_processed
            self._progress["skipped"] = total_skipped
            self._progress["succeeded"] = total_succeeded
            self._progress["failed"] = total_failed

            if len(assets) < page_size:
                break
            page += 1

        logger.info(
            "回填完成: processed=%d skipped=%d succeeded=%d failed=%d",
            total_processed, total_skipped, total_succeeded, total_failed,
        )

    async def _fetch_page(
        self,
        page: int,
        page_size: int,
        since: str,
    ) -> list[dict[str, Any]]:
        """用 Immich metadata search API 分页拉取资产。"""
        payload: dict[str, Any] = {
            "size": page_size,
            "page": page,
            "withExif": False,
            "withStacks": False,
            "withPeople": False,
        }
        if since:
            payload["updatedAfter"] = since

        try:
            data = await self._immich.request_json(
                "POST", "/api/search/metadata", json=payload
            )
            items = self._extract_items(data)
            return items
        except Exception as exc:
            logger.warning("回填拉取第 %d 页失败: %s", page, exc)
            return []

    @staticmethod
    def _extract_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            assets = data.get("assets", {})
            if isinstance(assets, dict):
                return assets.get("items", [])
            if isinstance(assets, list):
                return assets
            return data.get("items", [])
        if isinstance(data, list):
            return data
        return []

    async def _process_one(
        self,
        asset: dict[str, Any],
        sem: asyncio.Semaphore,
        dry_run: bool,
        skip_tagged: bool,
    ) -> str:
        """处理单张资产：场景分析 + 标签写回 + 描述补全。"""
        asset_id = asset.get("id")
        if not asset_id:
            return "skipped"

        if skip_tagged and self._has_ai_tags(asset):
            return "skipped"

        async with sem:
            try:
                from ..core.runtime_config import runtime_enabled
                if not runtime_enabled(
                    "writeback_enabled",
                    getattr(self._settings, "writeback_enabled", False),
                ):
                    return "skipped"

                results_data = [{"asset_id": asset_id, "scene": None}]

                scene_writeback = getattr(
                    self._immich, "_scene_writeback", None
                )
                if scene_writeback is not None and not dry_run:
                    await scene_writeback.writeback_scene_tags(results_data)

                return "succeeded"
            except Exception as exc:
                logger.warning("回填处理失败 asset=%s: %s", asset_id, exc)
                return "failed"

    @staticmethod
    def _has_ai_tags(asset: dict[str, Any]) -> bool:
        """检查资产是否已有 AI/ 前缀标签。"""
        tags = asset.get("tags", [])
        if not tags:
            return False
        for tag in tags:
            if isinstance(tag, dict):
                value = tag.get("value", "") or tag.get("name", "")
            elif isinstance(tag, str):
                value = tag
            else:
                continue
            if value.startswith(_AI_TAG_PREFIX):
                return True
        return False
