"""SQLAlchemy 评分数据访问具体实现。

使用 SQLAlchemy 异步会话实现 RatingRepository 接口，
UPSERT 操作通过 SQLite 的 INSERT ON CONFLICT DO UPDATE 原子性实现。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import PhotoRating
from .rating_base import RatingRepository

logger = logging.getLogger(__name__)


class SqlRatingRepository(RatingRepository):
    """基于 SQLAlchemy 异步会话的评分数据访问实现。

    每个方法自行管理会话生命周期（使用 async with），
    调用方无需关心会话的创建与关闭。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """初始化仓库实例。

        参数:
            session_factory: SQLAlchemy 异步会话工厂。
        """
        self._session_factory = session_factory

    async def upsert(
        self,
        asset_id: str,
        user_id: str,
        rating: int,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """原子性地插入或更新评分（INSERT ON CONFLICT DO UPDATE）。

        利用 SQLite 的 ON CONFLICT 子句在 (asset_id, user_id) 冲突时
        原子性地更新 rating、comment 和 updated_at，避免先查询再写入的两步操作。
        """
        stmt = sqlite_insert(PhotoRating).values(
            asset_id=asset_id,
            user_id=user_id,
            rating=rating,
            comment=comment,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["asset_id", "user_id"],
            set_={
                "rating": stmt.excluded.rating,
                "comment": stmt.excluded.comment,
            },
        )

        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

        result = await self.get(asset_id, user_id)
        if result is None:
            raise RuntimeError(f"UPSERT 后查询失败: asset_id={asset_id}, user_id={user_id}")
        return result

    async def get(self, asset_id: str, user_id: str) -> dict[str, Any] | None:
        """获取单条评分记录，不存在时返回 None。"""
        stmt = select(PhotoRating).where(
            PhotoRating.asset_id == asset_id,
            PhotoRating.user_id == user_id,
        )
        async with self._session_factory() as session:
            row = await session.scalar(stmt)
            if row is None:
                return None
            return self._to_dict(row)

    async def delete(self, asset_id: str, user_id: str) -> bool:
        """删除评分记录，返回是否成功删除。"""
        stmt = delete(PhotoRating).where(
            PhotoRating.asset_id == asset_id,
            PhotoRating.user_id == user_id,
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get_batch(
        self,
        asset_ids: list[str],
        user_id: str,
    ) -> dict[str, dict[str, Any]]:
        """批量获取评分记录，返回 {asset_id: 记录字典} 映射。

        使用单条 IN 查询一次性获取所有评分，避免 N+1 问题。
        """
        if not asset_ids:
            return {}
        stmt = select(PhotoRating).where(
            PhotoRating.asset_id.in_(asset_ids),
            PhotoRating.user_id == user_id,
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(stmt)).all()
            return {row.asset_id: self._to_dict(row) for row in rows}

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """获取评分统计信息。

        返回 total_rated, average_rating, distribution, top_rated。
        """
        async with self._session_factory() as session:
            total_stmt = select(func.count()).select_from(PhotoRating).where(
                PhotoRating.user_id == user_id
            )
            total_rated = await session.scalar(total_stmt) or 0

            avg_stmt = select(func.avg(PhotoRating.rating)).where(
                PhotoRating.user_id == user_id
            )
            average_rating = await session.scalar(avg_stmt) or 0.0

            dist_stmt = (
                select(PhotoRating.rating, func.count())
                .where(PhotoRating.user_id == user_id)
                .group_by(PhotoRating.rating)
            )
            dist_result = await session.execute(dist_stmt)
            distribution: dict[str, int] = {}
            for rating_val, count in dist_result:
                distribution[str(rating_val)] = count

            top_stmt = (
                select(PhotoRating)
                .where(PhotoRating.user_id == user_id)
                .order_by(PhotoRating.rating.desc(), PhotoRating.updated_at.desc())
                .limit(10)
            )
            top_rows = (await session.scalars(top_stmt)).all()
            top_rated = [self._to_dict(row) for row in top_rows]

        return {
            "total_rated": total_rated,
            "average_rating": round(float(average_rating), 2),
            "distribution": distribution,
            "top_rated": top_rated,
        }

    async def get_asset_ids_by_min_rating(
        self,
        min_rating: int,
        user_id: str,
        limit: int = 200,
    ) -> list[str]:
        """按最低评分阈值筛选资产 ID 列表，按评分降序排列。"""
        stmt = (
            select(PhotoRating.asset_id)
            .where(
                PhotoRating.user_id == user_id,
                PhotoRating.rating >= min_rating,
            )
            .order_by(PhotoRating.rating.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(stmt)).all()
            return list(rows)

    @staticmethod
    def _to_dict(row: PhotoRating) -> dict[str, Any]:
        """将 ORM 行转换为字典。"""
        return {
            "id": row.id,
            "asset_id": row.asset_id,
            "user_id": row.user_id,
            "rating": row.rating,
            "comment": row.comment,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
