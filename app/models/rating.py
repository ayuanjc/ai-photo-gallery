"""评分相关的 Pydantic 请求模型与响应模型。

定义照片评分的创建、更新、查询、统计等数据结构，
评分值范围为 1-5 的整数。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CreateRatingRequest(BaseModel):
    """创建评分请求。

    - asset_id: Immich 资产 ID（必填）。
    - rating: 评分值 1-5（必填）。
    - user_id: 用户标识（可选，默认 "default"）。
    - comment: 评论文本（可选，最长 1000 字符）。
    """

    asset_id: str = Field(..., min_length=1, max_length=128, description="Immich 资产 ID")
    rating: int = Field(..., ge=1, le=5, description="评分值 1-5")
    user_id: str = Field(default="default", max_length=128, description="用户标识")
    comment: str | None = Field(default=None, max_length=1000, description="评论文本")

    @field_validator("asset_id")
    @classmethod
    def _validate_asset_id(cls, value: str) -> str:
        """校验 asset_id 非空且去除 null 字节。"""
        value = value.replace("\x00", "").strip()
        if not value:
            raise ValueError("asset_id 不能为空")
        return value

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, value: str) -> str:
        """校验 user_id 去除 null 字节，空时回退为 default。"""
        value = value.replace("\x00", "").strip()
        return value if value else "default"


class UpdateRatingRequest(BaseModel):
    """更新评分请求。

    - rating: 新评分值 1-5（必填）。
    - comment: 新评论文本（可选，最长 1000 字符）。
    """

    rating: int = Field(..., ge=1, le=5, description="评分值 1-5")
    comment: str | None = Field(default=None, max_length=1000, description="评论文本")


class RatingResponse(BaseModel):
    """评分响应模型，表示单条评分记录。"""

    asset_id: str = Field(description="Immich 资产 ID")
    user_id: str = Field(description="用户标识")
    rating: int = Field(ge=1, le=5, description="评分值 1-5")
    comment: str | None = Field(default=None, description="评论文本")
    created_at: datetime | None = Field(default=None, description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")


class BatchRatingRequest(BaseModel):
    """批量获取评分请求。

    - asset_ids: 资产 ID 列表（必填、非空，最多 200 条）。
    - user_id: 用户标识（可选，默认 "default"）。
    """

    asset_ids: list[str] = Field(..., min_length=1, max_length=200, description="资产 ID 列表")
    user_id: str = Field(default="default", max_length=128, description="用户标识")

    @field_validator("asset_ids")
    @classmethod
    def _validate_asset_ids(cls, value: list[str]) -> list[str]:
        """校验 asset_ids 列表：每项非空、无重复、长度不超过 128。"""
        seen: set[str] = set()
        for aid in value:
            aid = aid.replace("\x00", "").strip()
            if not aid:
                raise ValueError("asset_id 不能为空字符串")
            if len(aid) > 128:
                raise ValueError(f"asset_id 长度不能超过 128 字符: {aid[:20]}...")
            if aid in seen:
                raise ValueError(f"asset_id 重复: {aid}")
            seen.add(aid)
        return value


class BatchRatingResponse(BaseModel):
    """批量获取评分响应。

    - ratings: 已评分的资产列表（按 asset_id → RatingResponse 映射）。
    - missing_asset_ids: 未找到评分记录的 asset_id 列表。
    """

    ratings: list[RatingResponse] = Field(default_factory=list, description="已评分记录列表")
    missing_asset_ids: list[str] = Field(default_factory=list, description="未评分的资产 ID 列表")


class RatingStatsResponse(BaseModel):
    """评分统计响应。

    - total_rated: 已评分资产总数。
    - average_rating: 平均评分（0-5，无评分时为 0.0）。
    - distribution: 各评分值的数量分布（key 为 1-5，value 为数量）。
    - top_rated: 评分最高的资产列表（最多 10 条）。
    """

    total_rated: int = Field(ge=0, description="已评分资产总数")
    average_rating: float = Field(ge=0.0, le=5.0, description="平均评分")
    distribution: dict[str, int] = Field(default_factory=dict, description="评分分布 {1: n, 2: n, ...}")
    top_rated: list[RatingResponse] = Field(default_factory=list, description="评分最高的资产")


class DeleteRatingResponse(BaseModel):
    """删除评分响应。"""

    asset_id: str = Field(description="资产 ID")
    user_id: str = Field(description="用户标识")
    deleted: bool = Field(description="是否成功删除")