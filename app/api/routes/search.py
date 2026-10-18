"""搜索相关路由。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request

logger = logging.getLogger(__name__)

from ...core.config import get_settings
from ...core.runtime_config import runtime_enabled
from ...models.request import FeedbackRequest, MultiConditionSearchRequest, SearchRequest
from ...models.response import (
    FeedbackResponse,
    MultiConditionSearchResponse,
    SearchResponse,
)
from ...services.feedback_service import FeedbackService, get_feedback_service
from ...services.multi_condition_service import MultiConditionSearchService
from ...services.search_service import SearchService
from ..deps import (
    get_multi_condition_service,
    get_search_service,
    rate_limit_guard,
    verify_api_key,
)
from .admin import feature_gate

router = APIRouter(
    tags=["search"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit_guard)],
)


@router.post(
    "/search/smart",
    response_model=SearchResponse,
    summary="AI 图像语义搜索",
    description="提交图库 asset_ids 列表与自然语言关键词，调用 Immich 语义搜索并做二次排序。",
)
async def smart_search(
    request: SearchRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    """语义搜索端点，支持评分附加与 min_rating 过滤 + 场景标签写回。"""
    response = await service.search(request)

    # 场景标签写回 + AI相册写回（BackgroundTask，非阻塞）。
    # 仅写回真实分析结果（source=analyzed）：查询推断/兜底的猜测场景只用于前端展示，
    # 写回会把猜测当事实污染图库（标签/描述/评分/相册）。
    if (
        not response.cached
        and runtime_enabled("writeback_enabled", getattr(get_settings(), "writeback_enabled", False))
    ):
        analyzed = [
            r for r in response.results if r.scene is not None and r.scene.source == "analyzed"
        ]
        if analyzed:
            results_data = [
                {"asset_id": r.asset_id, "scene": r.scene.model_dump()} for r in analyzed
            ]
            scene_writeback = getattr(http_request.app.state, "scene_writeback", None)
            if scene_writeback is not None:
                background_tasks.add_task(scene_writeback.writeback_scene_tags, results_data)
            album_writeback = getattr(http_request.app.state, "album_writeback", None)
            if album_writeback is not None:
                background_tasks.add_task(album_writeback.writeback_albums, results_data)
            desc_writeback = getattr(http_request.app.state, "description_writeback", None)
            if desc_writeback is not None:
                desc_data = [
                    {
                        "asset_id": r.asset_id,
                        "scene": r.scene.model_dump(),
                    }
                    for r in analyzed
                ]
                background_tasks.add_task(desc_writeback.writeback_descriptions, desc_data)

    # 评分附加与 min_rating 过滤
    rating_service = getattr(http_request.app.state, "rating_service", None)
    if rating_service is not None:
        asset_ids = [item.asset_id for item in response.results]
        if asset_ids:
            try:
                rating_map = await rating_service.get_rating_map(asset_ids, "default")
            except Exception as exc:
                logger.warning("批量查询评分失败，降级不附加评分: %s", exc)
                rating_map = {}

            for item in response.results:
                item.rating = rating_map.get(item.asset_id)

            if request.min_rating is not None:
                filtered = [item for item in response.results if item.rating is not None and item.rating >= request.min_rating]
                response.results = filtered
                response.total = len(filtered)

    return response


@router.post(
    "/search/multi-condition",
    response_model=MultiConditionSearchResponse,
    summary="复合条件搜索（贝叶斯降级检索）",
    description="按 person_id + location + query 复合搜索，严格交集为空时自动降级为加权评分合并。",
)
async def multi_condition_search(
    request: MultiConditionSearchRequest,
    service: MultiConditionSearchService = Depends(get_multi_condition_service),
) -> MultiConditionSearchResponse:
    return await service.search(request)

@router.post(
    "/search/feedback",
    response_model=FeedbackResponse,
    summary="搜索反馈记录",
    description="记录用户对搜索结果的点击/跳过行为，供排序时做微加权。",
    dependencies=[Depends(feature_gate("feedback_enabled"))],
)
async def record_feedback(
    body: FeedbackRequest,
    request: Request,
) -> FeedbackResponse:
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        return FeedbackResponse(
            recorded=False, degraded=True, warnings=["缓存不可用，反馈未记录"]
        )
    service = get_feedback_service()
    if service is None:
        service = FeedbackService(cache, get_settings())
    warnings: list[str] = []
    degraded = False
    try:
        recorded, reason = await service.record_feedback(
            body.asset_id, body.query, body.action, body.session_id
        )
        if not recorded and reason == "rate_limited":
            warnings.append("反馈频率超限，已忽略")
            return FeedbackResponse(recorded=False, degraded=False, warnings=warnings)
    except Exception:
        degraded = True
        warnings.append("反馈记录异常，已降级")
    return FeedbackResponse(recorded=not degraded, degraded=degraded, warnings=warnings)


# ---- 搜索建议与纠错（末尾追加，挂在同一个 router 下）----
from fastapi import Query

from ...core.config import Settings
from ...models.response import CorrectResponse, SuggestResponse
from ...services.suggest_service import get_suggest_service
from ..deps import get_settings_dep


@router.get(
    "/search/suggest",
    response_model=SuggestResponse,
    summary="搜索建议",
    description="基于场景词典与热门搜索词，返回前缀/包含匹配的建议词。空 q 时返回热门词 top N。",
    dependencies=[Depends(feature_gate("suggest_enabled"))],
)
async def suggest(
    q: str = Query(default="", max_length=500, description="搜索关键词"),
    limit: int = Query(default=10, ge=1, le=50, description="返回数量上限"),
    settings: Settings = Depends(get_settings_dep),
) -> SuggestResponse:
    service = get_suggest_service(settings)
    stripped = q.strip()
    if stripped:
        service.record_query(stripped)
    suggestions, degraded, warnings = service.suggest(q, limit)
    return SuggestResponse(suggestions=suggestions, degraded=degraded, warnings=warnings)


@router.get(
    "/search/correct",
    response_model=CorrectResponse,
    summary="搜索纠错",
    description="基于 Levenshtein 编辑距离对场景词典进行纠错匹配，距离 <= 阈值时返回建议词。",
    dependencies=[Depends(feature_gate("correct_enabled"))],
)
async def correct(
    q: str = Query(default="", max_length=500, description="搜索关键词"),
    settings: Settings = Depends(get_settings_dep),
) -> CorrectResponse:
    service = get_suggest_service(settings)
    corrected, confidence, applied, warnings = service.correct(q)
    return CorrectResponse(
        corrected=corrected, confidence=confidence, applied=applied,
        degraded=False, warnings=warnings,
    )

# ---- 自动相册（末尾追加，挂在同一个 router 下）----
from datetime import datetime as _datetime

from ...core.errors import ServiceUnavailableError
from ...models.response import AlbumListResponse
from ...services.album_service import AlbumService


@router.get(
    "/search/albums",
    response_model=AlbumListResponse,
    summary="自动相册生成",
    description="按场景 + 时间聚类，自动生成相册分组。无场景/无时间资产优雅跳过。",
)
async def list_albums(
    request: Request,
    date_from: _datetime | None = Query(default=None, description="起始时间（含）"),
    date_to: _datetime | None = Query(default=None, description="结束时间（含）"),
    settings: Settings = Depends(get_settings_dep),
) -> AlbumListResponse:
    if not settings.album_enabled:
        raise ServiceUnavailableError(
            "自动相册功能未启用，请配置 ALBUM_ENABLED=true"
        )

    cache = getattr(request.app.state, "cache", None)
    cache_key = (
        f"albums:{date_from.isoformat() if date_from else 'none'}:"
        f"{date_to.isoformat() if date_to else 'none'}"
    )

    if cache is not None:
        try:
            raw = await cache.get(cache_key)
            if raw is not None:
                resp = AlbumListResponse(**raw)
                resp.cached = True
                return resp
        except Exception as exc:
            logger.warning("相册缓存读取失败: %s", exc)

    immich_client = request.app.state.immich_client
    scene_analyzer = getattr(request.app.state, "scene_analyzer", None)
    service = AlbumService(settings, immich_client, scene_analyzer)

    try:
        albums, warnings = await service.generate_albums(
            date_from=date_from, date_to=date_to
        )
    except Exception as exc:
        return AlbumListResponse(
            total_albums=0,
            degraded=True,
            warnings=[f"相册生成异常，已降级: {exc}"],
        )


    response = AlbumListResponse(
        total_albums=len(albums),
        degraded=bool(warnings),
        warnings=warnings,
        albums=albums,
    )

    if cache is not None and not response.degraded:
        try:
            await cache.set(
                cache_key, response.model_dump(mode="json"),
                ttl=settings.album_cache_ttl_seconds,
            )
        except Exception as exc:
            logger.warning("相册缓存写入失败: %s", exc)

    return response
