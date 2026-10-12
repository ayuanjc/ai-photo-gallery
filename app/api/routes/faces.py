"""人脸分类相关路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...models.face import FaceClassifyRequest, FaceClassifyResponse
from ...services.face_service import FaceService
from ..deps import get_face_service, verify_api_key

router = APIRouter(tags=["faces"])


@router.post(
    "/faces/classify",
    response_model=FaceClassifyResponse,
    summary="人脸分类",
    description="对图片 URL 列表执行人脸检测与聚类，按人物分组返回（基于华为云 FRS）。",
    dependencies=[Depends(verify_api_key)],
)
async def classify_faces(
    request: FaceClassifyRequest,
    service: FaceService = Depends(get_face_service),
) -> FaceClassifyResponse:
    return await service.classify(request)
