"""历史资产回填服务：对已有 Immich 资产批量执行场景分析 + 标签写回。

解决"3 万张历史图永远不触发工作流"的问题。

流程：
1. 用 Immich metadata search API 分页拉取资产
2. skip_tagged：批量加载全部 AI· 标签下的资产 ID 集合，命中即跳过
   （metadata search 响应不含 tags 字段，逐资产查询代价过高）
3. 对每张资产跑真实场景分析（CLIP，source=analyzed），
   再经 SceneWriteBack 白名单（analyzed + 置信度下限）写回
4. 受并发与速率限制
5. 进度写内存 + Redis（跨重启可见），GET /admin/api/backfill/status 查询
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from ..core.config import Settings
from .immich import ImmichClient

if TYPE_CHECKING:
    from ..core.redis_client import RedisClient
    from ..scene.analyzer import SceneAnalyzer
    from .scene_writeback import SceneWriteBack

logger = logging.getLogger(__name__)

# 进度快照中 errors 列表上限：防上游持续故障时无界累积（10 万资产 × 200 字符 ≈ 20MB），
# 且每页全量序列化写 Redis 造成内存/带宽膨胀。failed 计数仍精确记录。
_MAX_PROGRESS_ERRORS = 200
_ERROR_TEXT_MAX = 200

_PROGRESS_KEY = "backfill:progress"
_AI_TAG_PREFIX = "AI·"


class BackfillService:
    def __init__(
        self,
        immich: ImmichClient,
        settings: Settings,
        scene_analyzer: SceneAnalyzer | None = None,
        scene_writeback: SceneWriteBack | None = None,
        redis_client: RedisClient | None = None,
    ) -> None:
        self._immich = immich
        self._settings = settings
        self._analyzer = scene_analyzer
        self._writeback = scene_writeback
        self._redis = redis_client
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

    def _public_progress(self) -> dict[str, Any]:
        return {k: v for k, v in self._progress.items() if not k.startswith("_")}

    async def get_status(self) -> dict[str, Any]:
        if self._running:
            self._progress["elapsed_seconds"] = round(
                time.monotonic() - self._progress.get("_t0", 0), 1
            )
            return self._public_progress()
        snapshot = await self._read_progress_redis()
        if snapshot is not None:
            return snapshot
        return self._public_progress()

    async def _save_progress_redis(self) -> None:
        """进度快照写 Redis（跨重启可见）；不可用仅记日志。"""
        if self._redis is None or not getattr(self._redis, "connected", False):
            return
        try:
            await self._redis.redis.set(
                _PROGRESS_KEY, json.dumps(self._public_progress(), ensure_ascii=False)
            )
        except Exception as exc:
            logger.warning("回填进度写 Redis 失败: %s", exc)

    async def _read_progress_redis(self) -> dict[str, Any] | None:
        """读取 Redis 进度快照（任务不在内存运行时的跨重启展示）。"""
        if self._redis is None or not getattr(self._redis, "connected", False):
            return None
        try:
            raw = await self._redis.redis.get(_PROGRESS_KEY)
            if raw is None:
                return None
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

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
            dry_run: 仅分析不实际写回。
            skip_tagged: 跳过已有 AI· 前缀标签的资产。
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
        await self._save_progress_redis()

        try:
            await self._do_backfill(limit, since, dry_run, skip_tagged)
            self._progress["status"] = "completed"
        except Exception as exc:
            logger.exception("回填任务异常: %s", exc)
            self._progress["status"] = "failed"
            if len(self._progress["errors"]) < _MAX_PROGRESS_ERRORS:
                self._progress["errors"].append(str(exc)[:_ERROR_TEXT_MAX])
        finally:
            self._progress["elapsed_seconds"] = round(
                time.monotonic() - self._progress.pop("_t0", 0), 1
            )
            self._running = False
            await self._save_progress_redis()

        return await self.get_status()

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

        skip_ids: set[str] = set()
        if skip_tagged:
            skip_ids = await self._load_tagged_asset_ids()

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
                self._process_one(asset, sem, dry_run, skip_ids)
                for asset in assets
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    total_failed += 1
                    if len(self._progress["errors"]) < _MAX_PROGRESS_ERRORS:
                        self._progress["errors"].append(str(result)[:_ERROR_TEXT_MAX])
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
            await self._save_progress_redis()

            if len(assets) < page_size:
                break
            page += 1

        logger.info(
            "回填完成: processed=%d skipped=%d succeeded=%d failed=%d",
            total_processed, total_skipped, total_succeeded, total_failed,
        )

    async def _load_tagged_asset_ids(self) -> set[str]:
        """批量加载所有 AI· 标签下的资产 ID（skip_tagged 的跳过集合）。

        任一标签查询失败仅记日志继续（降级：该标签下资产可能被重复处理，
        由写回白名单 + Redis SETNX 幂等兜底）。
        """
        ids: set[str] = set()
        try:
            data = await self._immich.request_json("GET", "/api/tags")
        except Exception as exc:
            logger.warning("回填加载标签列表失败（skip 集合为空）: %s", exc)
            return ids
        tags = data if isinstance(data, list) else []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            value = str(tag.get("value") or tag.get("name") or "")
            if not value.startswith(_AI_TAG_PREFIX):
                continue
            tag_id = tag.get("id")
            if not tag_id:
                continue
            try:
                assets = await self._immich.request_json(
                    "GET", f"/api/tags/{tag_id}/assets"
                )
            except Exception as exc:
                logger.warning("回填加载标签资产失败 tag=%s: %s", value, exc)
                continue
            items = self._extract_items(assets)
            for asset in items:
                asset_id = asset.get("id") if isinstance(asset, dict) else None
                if asset_id:
                    ids.add(str(asset_id))
        if ids:
            logger.info("回填 skip 集合加载完成: %d 个已打标资产", len(ids))
        return ids

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
        skip_ids: set[str],
    ) -> str:
        """处理单张资产：真实场景分析 → SceneWriteBack 白名单写回。"""
        asset_id = asset.get("id")
        if not asset_id:
            return "skipped"

        if asset_id in skip_ids:
            return "skipped"

        if not self._settings.writeback_enabled and not dry_run:
            return "skipped"

        if self._analyzer is None or self._writeback is None:
            return "skipped"

        async with sem:
            try:
                scene_tags = await self._analyzer.analyze(asset)
                if scene_tags is None or scene_tags.source != "analyzed":
                    return "skipped"

                result = {
                    "asset_id": asset_id,
                    "scene": scene_tags.model_dump(),
                }
                if not dry_run:
                    await self._writeback.writeback_scene_tags([result])
                return "succeeded"
            except Exception as exc:
                logger.warning("回填处理失败 asset=%s: %s", asset_id, exc)
                return "failed"
