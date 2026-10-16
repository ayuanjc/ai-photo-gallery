"""评分数据访问抽象基类。

定义评分数据访问的统一接口契约，业务层依赖此抽象而非具体实现，
便于后续切换数据库后端（如 PostgreSQL、MySQL）时只需新增实现类。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RatingRepository(ABC):
    """评分数据访问抽象接口。

    所有方法均为异步，具体实现负责管理数据库会话的生命周期。
    唯一约束：(asset_id, user_id) 组合唯一。
    """

    @abstractmethod
    async def upsert(
        self,
        asset_id: str,
        user_id: str,
        rating: int,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """原子性地插入或更新评分（UPSERT 语义）。

        若 (asset_id, user_id) 已存在则更新 rating 和 comment，否则插入新记录。

        参数:
            asset_id: Immich 资产 ID。
            user_id: 用户标识。
            rating: 评分值 1-5。
            comment: 评论文本（可选）。

        返回:
            操作后的评分记录字典，包含 id, asset_id, user_id, rating, comment, created_at, updated_at。
        """

    @abstractmethod
    async def get(self, asset_id: str, user_id: str) -> dict[str, Any] | None:
        """获取单条评分记录。

        参数:
            asset_id: Immich 资产 ID。
            user_id: 用户标识。

        返回:
            评分记录字典，不存在时返回 None。
        """

    @abstractmethod
    async def delete(self, asset_id: str, user_id: str) -> bool:
        """删除评分记录。

        参数:
            asset_id: Immich 资产 ID。
            user_id: 用户标识。

        返回:
            是否成功删除（记录不存在时返回 False）。
        """

    @abstractmethod
    async def get_batch(
        self,
        asset_ids: list[str],
        user_id: str,
    ) -> dict[str, dict[str, Any]]:
        """批量获取评分记录，避免 N+1 查询。

        参数:
            asset_ids: 资产 ID 列表。
            user_id: 用户标识。

        返回:
            {asset_id: 评分记录字典} 映射，未评分的 asset_id 不包含在结果中。
        """

    @abstractmethod
    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """获取评分统计信息。

        参数:
            user_id: 用户标识。

        返回:
            统计字典，包含 total_rated, average_rating, distribution, top_rated。
        """

    @abstractmethod
    async def get_asset_ids_by_min_rating(
        self,
        min_rating: int,
        user_id: str,
        limit: int = 200,
    ) -> list[str]:
        """按最低评分阈值筛选资产 ID 列表。

        用于"优质成片筛选"：先在本地数据库筛选出符合条件的 asset_id，
        再批量向 Immich 请求详情，避免逐条请求。

        参数:
            min_rating: 最低评分阈值（1-5）。
            user_id: 用户标识。
            limit: 返回数量上限。

        返回:
            符合条件的 asset_id 列表，按评分降序排列。
        """