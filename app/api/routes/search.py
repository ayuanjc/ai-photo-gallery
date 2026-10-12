"""搜索相关路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...core.config import get_settings
from ...models.request import FeedbackRequest, MultiConditionSearchRequest, SearchRequest
from ...models.response import (
    FeedbackResponse,
    MultiConditionSearchResponse,
    SearchResponse,
)
from ...services.feedback_service import FeedbackService
from ...services.multi_condition_service import MultiConditionSearchService
from ...services.search_service import SearchService
from ..deps import (
    get_multi_condition_service,
    get_search_service,
    rate_limit_guard,
    verify_api_key,
)

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
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    return await service.search(request)


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
    service = FeedbackService(cache, get_settings())
    warnings: list[str] = []
    degraded = False
    try:
        await service.record_feedback(body.asset_id, body.query, body.action)
    except Exception:
        degraded = True
        warnings.append("反馈记录异常，已降级")
    return FeedbackResponse(recorded=not degraded, degraded=degraded, warnings=warnings)
