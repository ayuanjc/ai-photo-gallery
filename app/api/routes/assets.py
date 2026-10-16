"""资产图片代理路由：解决前端无法直接访问 Immich 缩略图（需 API Key）的问题。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response

from ...core.errors import AppError, ErrorCode
from ...services.image_proxy import ImageProxyService, ThumbnailFetchError
from ..deps import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["assets"],
    dependencies=[Depends(verify_api_key)],
)


def _get_proxy_service(request: Request) -> ImageProxyService:
    svc = getattr(request.app.state, "image_proxy_service", None)
    if svc is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "图片代理服务未初始化")
    return svc


@router.get(
    "/assets/{asset_id}/thumbnail",
    summary="代理获取资产缩略图",
    description="通过本服务代理获取 Immich 缩略图，前端无需携带 Immich API Key。",
)
async def proxy_thumbnail(
    request: Request,
    asset_id: str = Path(..., description="Immich 资产 ID"),
    size: str = Query(default="preview", description="缩略图尺寸: thumbnail|preview|full"),
) -> Response:
    svc = _get_proxy_service(request)
    try:
        image_bytes, content_type, etag = await svc.fetch_thumbnail(asset_id, size)
    except ThumbnailFetchError as exc:
        raise AppError(ErrorCode.UPSTREAM_ERROR, f"缩略图获取失败: {exc}") from exc

    headers = {
        "Cache-Control": "public, max-age=86400",
    }
    if etag:
        headers["ETag"] = etag
    return Response(content=image_bytes, media_type=content_type, headers=headers)


@router.get(
    "/assets/{asset_id}/original",
    summary="代理获取资产原图",
    description="通过本服务代理获取 Immich 原图，前端无需携带 Immich API Key。",
)
async def proxy_original(
    request: Request,
    asset_id: str = Path(..., description="Immich 资产 ID"),
) -> Response:
    svc = _get_proxy_service(request)
    try:
        image_bytes, content_type, etag = await svc.fetch_original(asset_id)
    except ThumbnailFetchError as exc:
        raise AppError(ErrorCode.UPSTREAM_ERROR, f"原图获取失败: {exc}") from exc

    headers = {
        "Cache-Control": "public, max-age=3600",
    }
    if etag:
        headers["ETag"] = etag
    return Response(content=image_bytes, media_type=content_type, headers=headers)