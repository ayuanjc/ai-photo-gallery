"""批量导入元数据路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...models.batch_import import ImportRequest, ImportResponse
from ...services.importer import BatchImporter
from ..deps import get_batch_importer, rate_limit_guard, verify_api_key

router = APIRouter(
    tags=["import"], dependencies=[Depends(verify_api_key), Depends(rate_limit_guard)]
)


@router.post(
    "/import/batch",
    response_model=ImportResponse,
    summary="批量导入图片元数据",
    description="对服务器本地图片文件执行 EXIF 元数据提取并上传到 Immich，返回逐文件结果。",
)
async def batch_import(
    request: ImportRequest,
    service: BatchImporter = Depends(get_batch_importer),
) -> ImportResponse:
    return await service.import_files(request)
