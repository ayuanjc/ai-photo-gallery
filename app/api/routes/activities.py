"""活动台账路由：列出活动、看详情、把一句话解析成唯一活动。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...models.activity import (
    ActivityDetail,
    ActivityListResponse,
    ResolveRequest,
    ResolveResponse,
)
from ...services.workflow_service import WorkflowService
from ..deps import get_activity_store, get_workflow_service, verify_api_key

router = APIRouter(tags=["activities"], dependencies=[Depends(verify_api_key)])


@router.get(
    "/activities",
    response_model=ActivityListResponse,
    summary="列出活动台账",
    description="返回台账内全部活动摘要（含照片数量、时间窗、主办方）。台账是照片范围的事实来源。",
)
def list_activities(service: WorkflowService = Depends(get_workflow_service)) -> ActivityListResponse:
    data = service.list_activities()
    return ActivityListResponse(**data)


@router.get(
    "/activities/{activity_id}",
    response_model=ActivityDetail,
    summary="活动详情（含白名单 asset_ids）",
)
def get_activity(
    activity_id: str,
    service: WorkflowService = Depends(get_workflow_service),
) -> ActivityDetail:
    return ActivityDetail(**service.get_activity(activity_id))


@router.post(
    "/activities/resolve",
    response_model=ResolveResponse,
    summary="一句话/日期解析活动",
    description=(
        "别名精确 → 子串/近似（difflib≥0.72）→ 时间窗（±1 天）三级匹配。"
        "多命中返回 ambiguous + candidates，由调用方追问，绝不替用户猜场次。"
    ),
)
def resolve_activity(
    payload: ResolveRequest,
    service: WorkflowService = Depends(get_workflow_service),
) -> ResolveResponse:
    return ResolveResponse(**service.resolve(payload))


@router.post(
    "/activities/reload",
    summary="重新载入台账文件",
    description="台账 JSON 更新后无需重启服务。",
)
def reload_activities(store=Depends(get_activity_store)) -> dict:
    store.reload()
    return {"reloaded": True, "total": len(store.list()), "path": str(store.path)}
