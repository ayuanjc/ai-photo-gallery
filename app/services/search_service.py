"""搜索编排服务：串起视觉增强、Immich 语义搜索与二次排序。"""
from __future__ import annotations

import asyncio
import logging

from ..core.config import Settings
from ..core.errors import UpstreamTimeoutError, UpstreamUnavailableError
from ..models.request import SearchRequest
from ..models.response import SearchResultItem, SearchResponse
from ..scene.analyzer import SceneAnalyzer
from ..scene.models import SceneTags
from .immich import ImmichClient
from .ranking import calculate_dynamic_weights, clamp, rank_assets
from .vision.base import VisionProvider

logger = logging.getLogger(__name__)



class SearchService:
    def __init__(
        self,
        settings: Settings,
        immich: ImmichClient,
        vision: VisionProvider,
        scene_analyzer: SceneAnalyzer | None = None,
    ) -> None:
        self._settings = settings
        self._immich = immich
        self._vision = vision
        self._scene_analyzer = scene_analyzer

    async def search(self, request: SearchRequest) -> SearchResponse:
        # 1. 并发调用视觉识别，从图片 URL 提取标签，用于增强语义查询。
        enriched_query, warnings = await self._enrich_query(request)

        # 2. 调用 Immich 语义搜索（上游不可用/超时时降级返回空结果，不报 5xx）。
        try:
            assets = await self._immich.smart_search(enriched_query, k=request.limit, asset_ids=request.asset_ids)
        except (UpstreamUnavailableError, UpstreamTimeoutError) as exc:
            logger.error("Immich 不可用，降级返回空结果: %s", exc)
            return SearchResponse(
                query=request.query,
                total=0,
                degraded=True,
                warnings=["Immich 暂不可用，返回空结果"],
                results=[],
            )

        # 3. 为每条 asset 生成场景标签（失败不阻塞），并把 primary_scene 附加到 asset。
        await self._attach_scenes(assets)

        # 4. 解析单次请求的权重覆盖（否则用全局配置）。
        time_weight, similarity_weight = self._resolve_weights(request)

        # 5. 二次排序（权重按各 asset 的 primary_scene 动态选择）。
        ranked = rank_assets(
            assets,
            time_weight=time_weight,
            similarity_weight=similarity_weight,
            half_life_days=self._settings.time_decay_half_life_days,
            default_iso=self._settings.default_datetime_iso,
        )

        # 6. 组装结果。
        results = [
            self._to_item(
                asset,
                final=final,
                similarity=similarity,
                freshness=freshness,
                captured_at=captured_at,
                source=source,
                fields=request.include_metadata_fields,
                include_original=request.include_original_url,
            )
            for asset, final, similarity, freshness, captured_at, source in ranked[: request.limit]
        ]

        return SearchResponse(
            query=request.query,
            total=len(results),
            degraded=any("不可用" in w or "降级" in w for w in warnings),
            warnings=warnings,
            results=results,
            enriched_query=enriched_query if enriched_query != request.query else None,
        )

    async def _enrich_query(self, request: SearchRequest) -> tuple[str, list[str]]:
        """并发调用视觉识别，失败不阻塞主流程（降级记录 warning）。"""
        base = request.query
        semantic = asyncio.Semaphore(self._settings.search_max_concurrent_vision)

        async def _analyze(url: str) -> list[str]:
            async with semantic:
                try:
                    result = await self._vision.analyze(url)
                    return result.keywords()
                except Exception as exc:  # 视觉识别失败不应导致整体失败
                    logger.warning("视觉识别失败，url=%s err=%s", url, exc)
                    return []

        urls = [self._immich.thumbnail_url(aid) for aid in request.asset_ids]
        batches = await asyncio.gather(*(_analyze(u) for u in urls))
        keywords = [kw for batch in batches for kw in batch]

        if not keywords:
            return base, []

        enriched = self._vision.build_enriched_query(base, keywords)
        if enriched == base:
            return base, []
        return enriched, [f"已通过视觉识别增强查询词（{len(keywords)} 个标签）"]

    async def _attach_scenes(self, assets: list[dict]) -> None:
        """并发为每条 asset 生成场景标签，失败不阻塞搜索主链路。

        成功时在 asset 上附加 scene_tags 与 primary_scene；整体异常时仅记日志，
        asset 不携带 scene 信息（对应响应中 scene=None）。
        """
        if self._scene_analyzer is None or not self._settings.scene_enabled:
            return
        semantic = asyncio.Semaphore(self._settings.search_max_concurrent_scene)

        async def _one(asset: dict) -> None:
            async with semantic:
                try:
                    tags = await self._scene_analyzer.analyze(asset)
                except Exception as exc:  # 场景分析异常不应导致搜索失败
                    logger.warning("场景分析失败，asset_id=%s err=%s", asset.get("id"), exc)
                    return
                asset["scene_tags"] = tags
                asset["primary_scene"] = tags.primary_scene

        await asyncio.gather(*(_one(asset) for asset in assets))

    def _resolve_weights(self, request: SearchRequest) -> tuple[float, float]:
        time_weight = request.time_weight
        similarity_weight = request.similarity_weight
        if time_weight is not None or similarity_weight is not None:
            time_weight = time_weight if time_weight is not None else 1.0 - (similarity_weight or 0.0)
            similarity_weight = similarity_weight if similarity_weight is not None else 1.0 - (time_weight or 0.0)
            total = time_weight + similarity_weight
            if total > 0:
                return time_weight / total, similarity_weight / total
        base_alpha, base_beta = self._settings.weights
        return calculate_dynamic_weights(
            request.query,
            base_alpha=base_alpha,
            base_beta=base_beta,
            gamma=self._settings.query_intent_gamma,
        )

    def _to_item(
        self,
        asset: dict,
        *,
        final: float,
        similarity: float,
        freshness: float,
        captured_at,
        source: str,
        fields: list[str],
        include_original: bool,
    ) -> SearchResultItem:
        asset_id = str(asset.get("id", ""))
        metadata = self._extract_metadata(asset, fields)

        return SearchResultItem(
            asset_id=asset_id,
            thumbnail_url=self._immich.thumbnail_url(asset_id),
            original_url=self._immich.original_url(asset_id) if include_original else None,
            captured_at=captured_at,
            captured_at_source=source,
            similarity_score=round(clamp(similarity), 6),
            time_freshness=round(clamp(freshness), 6),
            final_score=round(final, 6),
            metadata=metadata,
            scene=asset.get("scene_tags"),
        )

    @staticmethod
    def _extract_metadata(asset: dict, fields: list[str]) -> dict:
        """将简化字段名映射到 Immich 实际 exif/asset 键。"""
        exif = asset.get("exifInfo") or {}
        metadata: dict = {}
        for field in fields:
            if field == "format":
                value = asset.get("originalMimeType")
            else:
                key = METADATA_EXIF_KEYS.get(field, field)
                value = exif.get(key)
            if value is not None:
                metadata[field] = value
        return metadata


# 简化字段名 -> Immich exifInfo 实际键
METADATA_EXIF_KEYS = {
    "width": "exifImageWidth",
    "height": "exifImageHeight",
    "latitude": "latitude",
    "longitude": "longitude",
    "city": "city",
    "state": "state",
    "country": "country",
    "make": "make",
    "model": "model",
    "lensModel": "lensModel",
    "iso": "iso",
    "exposureTime": "exposureTime",
    "fNumber": "fNumber",
    "focalLength": "focalLength",
    "orientation": "orientation",
    "projectionType": "projectionType",
}