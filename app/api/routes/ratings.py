"""评分管理路由：6 个 REST API 端点。

端点列表：
- POST   /ratings          — 创建评分（UPSERT 语义）
- PUT    /ratings/{asset_id} — 更新评分
- GET    /ratings/{asset_id} — 获取单条评分
- DELETE /ratings/{asset_id} — 删除评分
- POST   /ratings/batch    — 批量获取评分
- GET    /ratings/stats    — 获取评分统计
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ...core.errors import AppError, ErrorCode
from ...models.rating import (
    BatchRatingRequest,
    BatchRatingResponse,
    CreateRatingRequest,
    DeleteRatingResponse,
    RatingResponse,
    RatingStatsResponse,
    UpdateRatingRequest,
)
from ...services.rating_service import RatingService

router = APIRouter(tags=["rating"])


def _get_rating_service(request: Request) -> RatingService:
    """从应用状态获取评分服务实例。

    raises:
        AppError: 评分功能未启用时抛出 SERVICE_UNAVAILABLE。
    """
    service: RatingService | None = getattr(request.app.state, "rating_service", None)
    if service is None:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "评分功能未启用，请配置 RATING_ENABLED=true 并初始化数据库",
        )
    return service


@router.post(
    "/ratings",
    response_model=RatingResponse,
    summary="创建评分",
    description="对指定资产创建评分（UPSERT 语义：已存在则更新）。",
)
async def create_rating(
    body: CreateRatingRequest,
    request: Request,
) -> RatingResponse:
    """创建评分端点。

    使用 UPSERT 语义：若 (asset_id, user_id) 已存在则更新，否则插入。
    """
    service = _get_rating_service(request)
    return await service.create_or_update(
        asset_id=body.asset_id,
        user_id=body.user_id,
        rating=body.rating,
        comment=body.comment,
    )


@router.put(
    "/ratings/{asset_id}",
    response_model=RatingResponse,
    summary="更新评分",
    description="更新指定资产的评分（UPSERT 语义：不存在则创建）。",
)
async def update_rating(
    asset_id: str,
    body: UpdateRatingRequest,
    request: Request,
    user_id: str = Query(default="default", max_length=128, description="用户标识"),
) -> RatingResponse:
    """更新评分端点。

    使用 UPSERT 语义：若记录不存在则创建，存在则更新。
    """
    service = _get_rating_service(request)
    return await service.create_or_update(
        asset_id=asset_id,
        user_id=user_id,
        rating=body.rating,
        comment=body.comment,
    )


@router.get(
    "/ratings/stats",
    response_model=RatingStatsResponse,
    summary="获取评分统计",
    description="返回评分总数、平均分、分布和评分最高的资产列表。",
)
async def get_rating_stats(
    request: Request,
    user_id: str = Query(default="default", max_length=128, description="用户标识"),
) -> RatingStatsResponse:
    """获取评分统计端点。"""
    service = _get_rating_service(request)
    return await service.get_stats(user_id)


@router.post(
    "/ratings/batch",
    response_model=BatchRatingResponse,
    summary="批量获取评分",
    description="批量查询多个资产的评分记录，避免 N+1 查询。",
)
async def get_batch_ratings(
    body: BatchRatingRequest,
    request: Request,
) -> BatchRatingResponse:
    """批量获取评分端点。"""
    service = _get_rating_service(request)
    return await service.get_batch_ratings(body.asset_ids, body.user_id)


@router.get(
    "/ratings/{asset_id}",
    response_model=RatingResponse,
    summary="获取单条评分",
    description="查询指定资产的评分记录。",
)
async def get_rating(
    asset_id: str,
    request: Request,
    user_id: str = Query(default="default", max_length=128, description="用户标识"),
) -> RatingResponse:
    """获取单条评分端点。

    raises:
        AppError: 评分记录不存在时抛出 NOT_FOUND。
    """
    service = _get_rating_service(request)
    result = await service.get_rating(asset_id, user_id)
    if result is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"评分记录不存在: asset_id={asset_id}, user_id={user_id}",
        )
    return result


@router.delete(
    "/ratings/{asset_id}",
    response_model=DeleteRatingResponse,
    summary="删除评分",
    description="删除指定资产的评分记录。",
)
async def delete_rating(
    asset_id: str,
    request: Request,
    user_id: str = Query(default="default", max_length=128, description="用户标识"),
) -> DeleteRatingResponse:
    """删除评分端点。"""
    service = _get_rating_service(request)
    return await service.delete_rating(asset_id, user_id)