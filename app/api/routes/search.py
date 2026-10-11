"""搜索相关路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...models.request import SearchRequest
from ...models.response import SearchResponse
from ..deps import get_search_service, verify_api_key
from ...services.search_service import SearchService

router = APIRouter(tags=["search"], dependencies=[Depends(verify_api_key)])


@router.post(
    "/search/smart",
    response_model=SearchResponse,
    summary="AI 图像语义搜索",
    description="提交图片 URL 列表与自然语言关键词，调用 Immich 语义搜索并做二次排序。",
)
async def smart_search(
    request: SearchRequest,
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    return await service.search(request)