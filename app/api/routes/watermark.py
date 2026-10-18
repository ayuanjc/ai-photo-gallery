"""水印管理 API 路由。

提供预置模板查询、授权类型枚举、以及批量应用水印接口。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...models.watermark import (
    LicenseType,
    WatermarkRequest,
    WatermarkResponse,
)
from ...services.watermark_processor import BatchWatermarkProcessor
from ...services.watermark_templates import LICENSE_TEXT, available_templates, resolve
from ..deps import get_watermark_processor, rate_limit_guard, verify_api_key

router = APIRouter(
    tags=["watermark"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit_guard)],
)


@router.get(
    "/watermark/templates",
    summary="列出预置水印模板",
    description="返回所有预置水印模板及其样式定义，供前端选择。",
)
async def list_templates() -> dict:
    """列出全部预置水印模板。"""
    templates: list[dict] = []
    for name in available_templates():
        template = resolve(name)
        templates.append(
            {
                "name": template.name,
                "license_type": template.license_type.value,
                "text": template.text.model_dump(mode="json") if template.text else None,
                "image": template.image.model_dump(mode="json") if template.image else None,
                "tile": template.tile.model_dump(mode="json") if template.tile else None,
            }
        )
    return {"templates": templates}


@router.get(
    "/watermark/licenses",
    summary="列出授权类型",
    description="返回所有可用的授权类型及其默认版权文案。",
)
async def list_licenses() -> dict:
    """列出所有授权类型枚举及对应文案。"""
    return {
        "licenses": [
            {"type": license_type.value, "text": LICENSE_TEXT[license_type]}
            for license_type in LicenseType
        ]
    }


@router.post(
    "/watermark/apply",
    response_model=WatermarkResponse,
    summary="批量应用水印",
    description="对服务器本地图片批量应用文字/图片水印，返回逐文件结果。",
)
async def apply_watermark(
    request: WatermarkRequest,
    processor: BatchWatermarkProcessor = Depends(get_watermark_processor),
) -> WatermarkResponse:
    """批量应用水印并返回逐文件处理结果。"""
    return await processor.process(request)
