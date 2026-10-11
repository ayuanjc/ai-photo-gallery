"""搜索相关路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...models.request import MultiConditionSearchRequest, SearchRequest
from ...models.response import MultiConditionSearchResponse, SearchResponse
from ..deps import get_multi_condition_service, get_search_service, verify_api_key
from ...services.multi_condition_service import MultiConditionSearchService
from ...services.search_service import SearchService

router = APIRouter(tags=["search"], dependencies=[Depends(verify_api_key)])


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