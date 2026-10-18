"""评分业务服务层。

封装评分的业务逻辑，调用 RatingRepository 进行数据访问，
并将 repository 返回的字典转换为 Pydantic 响应模型。
"""
from __future__ import annotations

import logging
from typing import Any

from ..models.rating import (
    BatchRatingResponse,
    DeleteRatingResponse,
    RatingResponse,
    RatingStatsResponse,
)
from ..repositories.rating_base import RatingRepository

logger = logging.getLogger(__name__)


class RatingService:
    """评分业务服务，编排数据访问与响应模型转换。

    依赖 RatingRepository 抽象接口，不绑定具体数据库实现。
    """

    def __init__(self, repository: RatingRepository) -> None:
        """初始化评分服务。

        参数:
            repository: 评分数据访问仓库实例。
        """
        self._repo = repository

    async def create_or_update(
        self,
        asset_id: str,
        user_id: str,
        rating: int,
        comment: str | None = None,
    ) -> RatingResponse:
        """创建或更新评分（UPSERT 语义）。

        参数:
            asset_id: Immich 资产 ID。
            user_id: 用户标识。
            rating: 评分值 1-5。
            comment: 评论文本（可选）。

        返回:
            评分响应模型。
        """
        data = await self._repo.upsert(asset_id, user_id, rating, comment)
        return self._to_response(data)

    async def get_rating(self, asset_id: str, user_id: str) -> RatingResponse | None:
        """获取单条评分，不存在时返回 None。

        参数:
            asset_id: Immich 资产 ID。
            user_id: 用户标识。

        返回:
            评分响应模型或 None。
        """
        data = await self._repo.get(asset_id, user_id)
        if data is None:
            return None
        return self._to_response(data)

    async def delete_rating(self, asset_id: str, user_id: str) -> DeleteRatingResponse:
        """删除评分记录。

        参数:
            asset_id: Immich 资产 ID。
            user_id: 用户标识。

        返回:
            删除响应模型，含 deleted 字段表示是否成功。
        """
        deleted = await self._repo.delete(asset_id, user_id)
        return DeleteRatingResponse(asset_id=asset_id, user_id=user_id, deleted=deleted)

    async def get_batch_ratings(
        self,
        asset_ids: list[str],
        user_id: str,
    ) -> BatchRatingResponse:
        """批量获取评分，避免 N+1 查询。

        参数:
            asset_ids: 资产 ID 列表。
            user_id: 用户标识。

        返回:
            批量评分响应模型，含 ratings 列表和 missing_asset_ids 列表。
        """
        mapping = await self._repo.get_batch(asset_ids, user_id)
        ratings = [self._to_response(data) for data in mapping.values()]
        missing = [aid for aid in asset_ids if aid not in mapping]
        return BatchRatingResponse(ratings=ratings, missing_asset_ids=missing)

    async def get_stats(self, user_id: str) -> RatingStatsResponse:
        """获取评分统计信息。

        参数:
            user_id: 用户标识。

        返回:
            评分统计响应模型。
        """
        data = await self._repo.get_stats(user_id)
        top_rated = [self._to_response(item) for item in data.get("top_rated", [])]
        return RatingStatsResponse(
            total_rated=data.get("total_rated", 0),
            average_rating=data.get("average_rating", 0.0),
            distribution=data.get("distribution", {}),
            top_rated=top_rated,
        )

    async def get_asset_ids_by_min_rating(
        self,
        min_rating: int,
        user_id: str,
        limit: int = 200,
    ) -> list[str]:
        """按最低评分阈值筛选资产 ID 列表。

        用于"优质成片筛选"策略：先在本地数据库筛选出符合条件的 asset_id，
        再由调用方批量向 Immich 请求详情。

        参数:
            min_rating: 最低评分阈值（1-5）。
            user_id: 用户标识。
            limit: 返回数量上限。

        返回:
            符合条件的 asset_id 列表，按评分降序排列。
        """
        return await self._repo.get_asset_ids_by_min_rating(min_rating, user_id, limit)

    async def get_rating_map(
        self,
        asset_ids: list[str],
        user_id: str,
    ) -> dict[str, int]:
        """批量获取评分映射 {asset_id: rating}，供搜索结果附加评分使用。

        避免在遍历搜索结果时逐条查询评分（N+1 问题）。

        参数:
            asset_ids: 资产 ID 列表。
            user_id: 用户标识。

        返回:
            {asset_id: rating_value} 映射，未评分的 asset_id 不包含在内。
        """
        mapping = await self._repo.get_batch(asset_ids, user_id)
        return {aid: data["rating"] for aid, data in mapping.items()}

    @staticmethod
    def _to_response(data: dict[str, Any]) -> RatingResponse:
        """将 repository 返回的字典转换为 RatingResponse 模型。"""
        return RatingResponse(
            asset_id=data["asset_id"],
            user_id=data["user_id"],
            rating=data["rating"],
            comment=data.get("comment"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
