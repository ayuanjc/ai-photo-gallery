"""搜索编排服务：串起视觉增强、Immich 语义搜索与二次排序。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from ..core.cache import CacheBackend
from ..core.config import Settings
from ..core.errors import UpstreamTimeoutError, UpstreamUnavailableError, VisionError
from ..core.runtime_config import runtime_enabled
from ..models.request import SearchRequest
from ..models.response import SearchResponse, SearchResultItem
from ..scene.analyzer import SceneAnalyzer
from .explain import build_explanation
from .immich import ImmichClient
from .immich_capabilities import ImmichCapabilities
from .ranking import (
    apply_sort_by,
    blend_scene_weights,
    calculate_dynamic_weights,
    clamp,
    fuse_multimodal_query,
    rank_assets,
)
from .dedup import dedup_items
from .image_query_service import ImageQueryService
from .negation_parser import filter_by_negations, parse_negations
from .time_parser import filter_by_time_range, parse_time_hint
from .vision.base import VisionProvider

logger = logging.getLogger(__name__)



class SearchService:
    def __init__(
        self,
        settings: Settings,
        immich: ImmichClient,
        vision: VisionProvider,
        scene_analyzer: SceneAnalyzer | None = None,
        cache: CacheBackend | None = None,
        cache_ttl_seconds: float = 300.0,
        capabilities: ImmichCapabilities | None = None,
    ) -> None:
        self._settings = settings
        self._immich = immich
        self._vision = vision
        self._scene_analyzer = scene_analyzer
        self._cache = cache
        self._cache_ttl = cache_ttl_seconds
        self._capabilities = capabilities
        self._sem_vision = asyncio.Semaphore(settings.search_max_concurrent_vision)
        self._sem_scene = asyncio.Semaphore(settings.search_max_concurrent_scene)

    async def search(self, request: SearchRequest) -> SearchResponse:
        # 0. 查缓存：命中直接返回（cached=true），不调 Immich。
        cache_key: str | None = None
        if self._cache is not None:
            try:
                cache_key = self._cache_key(request)
                raw = await self._cache.get(cache_key)
                if raw is not None:
                    response = SearchResponse(**raw)
                    response.cached = True
                    return response
            except Exception as exc:
                logger.warning("搜索缓存读取失败，降级不缓存：err=%s", exc)
                cache_key = None

        # 1. 并发调用视觉识别，从图片 URL 提取标签，用于增强语义查询。
        #    时间语义化 + 否定语义搜索：先解析时间表达，再解析否定前缀，用清理后的 query 走搜索。
        cleaned_query = request.query
        applied_negations: list[str] = list(request.negations)
        applied_time_range: dict | None = None
        time_filter_range: tuple | None = None

        if runtime_enabled("time_parser_enabled", self._settings.time_parser_enabled):
            try:
                if request.time_hint:
                    hint_query, hint_range = parse_time_hint(request.time_hint)
                    if hint_range is not None:
                        time_filter_range = hint_range
                        applied_time_range = {
                            "date_from": hint_range[0].isoformat(),
                            "date_to": hint_range[1].isoformat(),
                            "source": "explicit",
                        }
                if time_filter_range is None:
                    cleaned_query, parsed_range = parse_time_hint(cleaned_query)
                    if parsed_range is not None:
                        time_filter_range = parsed_range
                        applied_time_range = {
                            "date_from": parsed_range[0].isoformat(),
                            "date_to": parsed_range[1].isoformat(),
                            "source": "natural_language",
                        }
            except Exception as exc:
                logger.warning("时间解析失败，降级为原 query: %s", exc)
                cleaned_query = request.query

        if runtime_enabled("negation_enabled", self._settings.negation_enabled):
            try:
                cleaned_query, parsed_negations = parse_negations(
                    cleaned_query, self._settings.negation_prefixes
                )
                if parsed_negations:
                    for tok in parsed_negations:
                        if tok not in applied_negations:
                            applied_negations.append(tok)
            except Exception as exc:
                logger.warning("否定解析失败，降级为原 query: %s", exc)

        if not cleaned_query.strip():
            cleaned_query = request.query

        enriched_query, warnings = await self._enrich_query(request, base_query=cleaned_query)

        # 1b. 以图搜图：若请求携带 image_urls，从参考图提取相似 asset_id 并合并到候选集。
        image_query_used = False
        effective_asset_ids = list(request.asset_ids)
        if request.image_urls and runtime_enabled(
            "image_query_enabled", self._settings.image_query_enabled
        ):
            image_service = ImageQueryService(self._settings, self._immich)
            try:
                image_asset_ids, image_warnings = await image_service.resolve_asset_ids_from_images(
                    request.image_urls, top_k=self._settings.image_query_top_k
                )
            except Exception as exc:
                logger.warning("以图搜图异常，降级为纯文本搜索: %s", exc)
                image_asset_ids = set()
                image_warnings = [f"以图搜图异常，已降级: {exc}"]

            if image_asset_ids:
                merged = set(effective_asset_ids) | image_asset_ids
                effective_asset_ids = list(merged)
                image_query_used = True
            warnings.extend(image_warnings)

        # 2. 调用 Immich 语义搜索（上游不可用/超时时降级返回空结果，不报 5xx）。
        try:
            assets = await self._immich.smart_search(
                enriched_query, k=request.limit, asset_ids=effective_asset_ids
            )
        except (UpstreamUnavailableError, UpstreamTimeoutError) as exc:
            logger.error("Immich 不可用，降级返回空结果: %s", exc)
            return SearchResponse(
                query=request.query,
                total=0,
                degraded=True,
                warnings=["Immich 暂不可用，返回空结果"],
                results=[],
            )

        # 2b. 能力降级：若 Immich 不支持 assetIds 参数（zod 剥离），
        #     上游会忽略 assetIds 返回全库结果，需本地交集过滤。
        if (
            effective_asset_ids
            and self._capabilities is not None
            and not self._capabilities.supports_asset_ids_in_search
        ):
            allowed = set(effective_asset_ids)
            before_count = len(assets)
            assets = [a for a in assets if a.get("id") in allowed]
            logger.info(
                "assetIds 不被上游支持，本地交集过滤: %d → %d",
                before_count,
                len(assets),
            )

        # 3. 为每条 asset 生成场景标签（失败不阻塞），并把 primary_scene 附加到 asset。
        #    时间语义过滤：在排序之前按时间范围过滤结果。
        if time_filter_range is not None:
            date_from, date_to = time_filter_range
            assets = filter_by_time_range(
                assets, date_from, date_to, self._settings.default_datetime_iso
            )

        await self._attach_scenes(assets)

        # 3b. 否定语义过滤：在排序之前过滤掉场景标签命中否定词的结果。
        negation_removed = 0
        if applied_negations and runtime_enabled(
            "negation_enabled", self._settings.negation_enabled
        ):
            before = len(assets)
            assets = filter_by_negations(assets, applied_negations)
            negation_removed = before - len(assets)

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

        # 6. 组装结果（去重在后、排序在前，保证保留高分图；EXPLAIN_ENABLED 时附加排序解释）。
        dedup_input = [
            {
                "asset": asset,
                "final_score": final,
                "similarity": similarity,
                "freshness": freshness,
                "captured_at": captured_at,
                "source": source,
            }
            for asset, final, similarity, freshness, captured_at, source in ranked[: request.limit]
        ]

        dedup_removed = 0
        if runtime_enabled("dedup_enabled", self._settings.dedup_enabled):
            try:
                dedup_input, dedup_removed = dedup_items(
                    dedup_input,
                    time_window_sec=self._settings.dedup_time_window_seconds,
                    similarity_threshold=self._settings.dedup_similarity_threshold,
                )
            except Exception as exc:
                logger.warning("去重失败，降级为不去重: %s", exc)
                dedup_removed = 0

        # 4b. 排序维度切换（去重之后、组装结果之前；关闭或 relevance 时跳过）。
        if request.sort_by != "relevance" and runtime_enabled(
            "sort_by_enabled", self._settings.sort_by_enabled
        ):
            try:
                dedup_input = apply_sort_by(
                    dedup_input,
                    request.sort_by,
                    random_seed=self._settings.sort_by_random_seed,
                )
            except Exception as exc:
                logger.warning("排序维度切换失败，保持原顺序: %s", exc)

        explain_enabled = runtime_enabled(
            "explain_enabled", self._settings.explain_enabled
        )
        results = []
        for item in dedup_input:
            asset = item["asset"]
            final = item["final_score"]
            similarity = item["similarity"]
            freshness = item["freshness"]
            captured_at = item["captured_at"]
            source = item["source"]
            explanation = None
            if explain_enabled:
                try:
                    alpha, beta = blend_scene_weights(
                        asset.get("scenes") or [asset.get("primary_scene")],
                        default_alpha=time_weight,
                        default_beta=similarity_weight,
                    )
                    explanation = build_explanation(
                        asset, similarity, freshness, (alpha, beta), asset.get("scene_tags")
                    )
                except Exception as exc:
                    logger.warning("解释生成失败，跳过该条解释: %s", exc)
                    explanation = None
            results.append(
                self._to_item(
                    asset,
                    final=final,
                    similarity=similarity,
                    freshness=freshness,
                    captured_at=captured_at,
                    source=source,
                    fields=request.include_metadata_fields,
                    include_original=request.include_original_url,
                    explanation=explanation,
                )
            )

        response = SearchResponse(
            query=request.query,
            total=len(results),
            degraded=bool(warnings),
            warnings=warnings,
            results=results,
            enriched_query=enriched_query if enriched_query != cleaned_query else None,
            dedup_removed=dedup_removed,
            image_query_used=image_query_used,
            applied_negations=applied_negations if applied_negations else [],
            applied_time_range=applied_time_range,
        )

        # 写缓存：仅非降级结果写入，降级结果不缓存。
        if cache_key is not None and not response.degraded:
            try:
                await self._cache.set(
                    cache_key, response.model_dump(mode="json"), ttl=self._cache_ttl
                )
            except Exception as exc:
                logger.warning("搜索缓存写入失败，不影响结果：err=%s", exc)

        return response

    def _cache_key(self, request: SearchRequest) -> str:
        """生成搜索缓存 key，包含所有影响结果的参数。"""
        payload = json.dumps(
            {
                "query": request.query,
                "asset_ids": request.asset_ids,
                "image_urls": request.image_urls,
                "negations": request.negations,
                "time_hint": request.time_hint,
                "limit": request.limit,
                "time_weight": request.time_weight,
                "similarity_weight": request.similarity_weight,
                "include_metadata_fields": request.include_metadata_fields,
                "include_original_url": request.include_original_url,
                "sort_by": request.sort_by,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"search:{digest}"

    async def _enrich_query(
        self, request: SearchRequest, base_query: str | None = None
    ) -> tuple[str, list[str]]:
        """并发调用视觉识别，失败不阻塞主流程（降级记录 warning）。"""
        base = base_query if base_query is not None else request.query
        semantic = self._sem_vision

        async def _analyze(url: str) -> list[str]:
            async with semantic:
                try:
                    result = await self._vision.analyze(url)
                    return result.keywords()
                except Exception as exc:
                    logger.warning("视觉识别失败，url=%s err=%s", url, exc)
                    return []

        urls = [self._immich.thumbnail_url(aid) for aid in request.asset_ids]
        batches = await asyncio.gather(*(_analyze(u) for u in urls), return_exceptions=True)
        keywords: list[str] = []
        for batch in batches:
            if isinstance(batch, Exception):
                logger.warning("视觉识别任务异常: %s", batch)
                continue
            keywords.extend(batch)

        if not keywords:
            return base, []

        enriched = fuse_multimodal_query(base, keywords, self._settings.multimodal_lambda)
        if enriched == base or not enriched:
            return base, []
        return enriched, [f"已通过视觉识别增强查询词（{len(keywords)} 个标签）"]

    async def _attach_scenes(self, assets: list[dict]) -> None:
        """并发为每条 asset 生成场景标签，失败不阻塞搜索主链路。

        成功时在 asset 上附加 scene_tags 与 primary_scene；整体异常时仅记日志，
        asset 不携带 scene 信息（对应响应中 scene=None）。
        """
        if self._scene_analyzer is None or not runtime_enabled(
            "scene_enabled", self._settings.scene_enabled
        ):
            return
        semantic = self._sem_scene

        async def _one(asset: dict) -> None:
            async with semantic:
                try:
                    tags = await self._scene_analyzer.analyze(asset)
                except Exception as exc:
                    logger.warning("场景分析失败，asset_id=%s err=%s", asset.get("id"), exc)
                    return
                asset["scene_tags"] = tags
                asset["primary_scene"] = tags.primary_scene

        results = await asyncio.gather(*(_one(asset) for asset in assets), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("场景分析任务异常: %s", r)

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
        explanation: dict | None = None,
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
            final_score=round(clamp(final), 6),
            metadata=metadata,
            scene=asset.get("scene_tags"),
            explanation=explanation,
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
