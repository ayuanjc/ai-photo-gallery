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
)
async def correct(
    q: str = Query(default="", max_length=500, description="搜索关键词"),
    settings: Settings = Depends(get_settings_dep),
) -> CorrectResponse:
    service = get_suggest_service(settings)
    corrected, degraded, warnings = service.correct(q)
    return CorrectResponse(corrected=corrected, degraded=degraded, warnings=warnings)
