"""工作流路由：活动内找图 → 素材包 → 编辑 → 导出。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from ...models.activity import (
    PackageEditRequest,
    PackageEditResponse,
    ResolveRequest,
    ResolveResponse,
    SearchInActivityRequest,
)
from ...services.workflow_service import WorkflowService
from ..deps import get_workflow_service, rate_limit_guard, verify_api_key

router = APIRouter(
    tags=["workflow"],
    dependencies=[Depends(verify_api_key)],
)


@router.get(
    "/workflow/resolve-activity",
    response_model=ResolveResponse,
    summary="活动解析（GET 便捷版）",
)
def resolve_activity(
    query: str | None = Query(default=None, max_length=200),
    date: str | None = Query(default=None, max_length=40),
    name: str | None = Query(default=None, max_length=100),
    service: WorkflowService = Depends(get_workflow_service),
) -> ResolveResponse:
    return ResolveResponse(**service.resolve(ResolveRequest(utterance=query, date=date, name=name)))


@router.post(
    "/workflow/search-in-activity",
    response_model=dict,
    summary="在活动范围内找图并生成素材包",
    description=(
        "检索永远带台账白名单（活动照片集合），活动名不进 CLIP 查询串；"
        "返回按用途分组的素材包（cover/body），每张图带可验证理由（场景/时段/尺寸/池内位次）。"
    ),
    dependencies=[Depends(rate_limit_guard)],
)
async def search_in_activity(
    payload: SearchInActivityRequest,
    service: WorkflowService = Depends(get_workflow_service),
) -> dict:
    return await service.search_in_activity(payload)


@router.get(
    "/workflow/package/{session_id}",
    response_model=dict,
    summary="读取素材包会话",
)
def get_package(session_id: str, service: WorkflowService = Depends(get_workflow_service)) -> dict:
    return service.view_session(session_id)


@router.post(
    "/workflow/package/edit",
    response_model=PackageEditResponse,
    summary="编辑素材包（池内重排，不重跑检索）",
    description="支持 replace（换横图/竖图）、drop（去掉这张）、add、set_slot；revision 单调递增，冲突即拒。",
)
def edit_package(
    payload: PackageEditRequest,
    service: WorkflowService = Depends(get_workflow_service),
) -> PackageEditResponse:
    return PackageEditResponse(**service.edit_package(payload))


@router.post(
    "/workflow/package/{session_id}/export",
    summary="导出素材包（manifest.csv + manifest.json + 缩略图 zip）",
    response_class=Response,
)
async def export_package(
    session_id: str,
    service: WorkflowService = Depends(get_workflow_service),
) -> Response:
    data, filename = await service.export_package(session_id)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
