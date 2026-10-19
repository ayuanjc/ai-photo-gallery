"""人脸分类相关路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.errors import AppError, ErrorCode
from ...models.face import FaceClassifyRequest, FaceClassifyResponse
from ...services.face_service import FaceService
from ...utils.url_safety import ensure_urls_dns_safe
from ..deps import get_face_service, rate_limit_guard, verify_api_key

router = APIRouter(
    tags=["faces"], dependencies=[Depends(verify_api_key), Depends(rate_limit_guard)]
)


@router.post(
    "/faces/classify",
    response_model=FaceClassifyResponse,
    summary="人脸分类",
    description="对图片 URL 列表执行人脸检测与聚类，按人物分组返回（基于华为云 FRS）。",
)
async def classify_faces(
    request: FaceClassifyRequest,
    service: FaceService = Depends(get_face_service),
) -> FaceClassifyResponse:
    # DNS 级 SSRF 校验（异步线程并发，fail-closed，不阻塞事件循环）。
    try:
        await ensure_urls_dns_safe(str(u) for u in request.image_urls)
    except ValueError as exc:
        raise AppError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
    return await service.classify(request)
