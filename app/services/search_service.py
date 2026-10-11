"""搜索编排服务：串起视觉增强、Immich 语义搜索与二次排序。"""
from __future__ import annotations

import asyncio
import logging

from ..core.config import Settings
from ..models.request import SearchRequest
from ..models.response import SearchResultItem, SearchResponse
from .immich import ImmichClient
from .ranking import clamp, rank_assets
from .vision.base import VisionProvider

logger = logging.getLogger(__name__)

# 控制视觉识别并发度，避免打爆上游。
_MAX_CONCURRENT_VISION = 5


class SearchService:
    def __init__(
        self,
        settings: Settings,
        immich: ImmichClient,
        vision: VisionProvider,
    ) -> None:
        self._settings = settings
        self._immich = immich
        self._vision = vision

    async def search(self, request: SearchRequest) -> SearchResponse:
        # 1. 并发调用视觉识别，从图片 URL 提取标签，用于增强语义查询。
        enriched_query, warnings = await self._enrich_query(request)

        # 2. 调用 Immich 语义搜索。
        assets = await self._immich.smart_search(enriched_query, k=request.limit)

        # 3. 解析单次请求的权重覆盖（否则用全局配置）。
        time_weight, similarity_weight = self._resolve_weights(request)

        # 4. 二次排序。
        ranked = rank_assets(
            assets,
            time_weight=time_weight,
            similarity_weight=similarity_weight,
            half_life_days=self._settings.time_decay_half_life_days,
            default_iso=self._settings.default_datetime_iso,
        )

        # 5. 组装结果。
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
            degraded=bool(warnings),
            warnings=warnings,
            results=results,
            enriched_query=enriched_query if enriched_query != request.query else None,
        )

    async def _enrich_query(self, request: SearchRequest) -> tuple[str, list[str]]:
        """并发调用视觉识别，失败不阻塞主流程（降级记录 warning）。"""
        base = request.query
        semantic = asyncio.Semaphore(_MAX_CONCURRENT_VISION)

        async def _analyze(url: str) -> list[str]:
            async with semantic:
                try:
                    result = await self._vision.analyze(url)
                    return result.keywords()
                except Exception as exc:  # 视觉识别失败不应导致整体失败
                    logger.warning("视觉识别失败，url=%s err=%s", url, exc)
                    return []

        urls = [str(u) for u in request.image_urls]
        batches = await asyncio.gather(*(_analyze(u) for u in urls))
        keywords = [kw for batch in batches for kw in batch]

        if not keywords:
            return base, []

        enriched = self._vision.build_enriched_query(base, keywords)
        if enriched == base:
            return base, []
        return enriched, [f"已通过视觉识别增强查询词（{len(keywords)} 个标签）"]

    def _resolve_weights(self, request: SearchRequest) -> tuple[float, float]:
        time_weight = request.time_weight
        similarity_weight = request.similarity_weight
        if time_weight is not None or similarity_weight is not None:
            time_weight = time_weight if time_weight is not None else 1.0 - (similarity_weight or 0.0)
            similarity_weight = similarity_weight if similarity_weight is not None else 1.0 - (time_weight or 0.0)
            total = time_weight + similarity_weight
            if total > 0:
                return time_weight / total, similarity_weight / total
        return self._settings.weights

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