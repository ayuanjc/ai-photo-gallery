"""SQLAlchemy 异步数据库引擎与 ORM 模型。

提供：
- 异步引擎与会话工厂（async_sessionmaker）。
- PhotoRating ORM 模型（评分持久化表，唯一约束 asset_id + user_id）。
- 建表逻辑（init_db / close_db），供应用 lifespan 调用。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类，所有 ORM 模型继承此类。"""


class PhotoRating(Base):
    """照片评分 ORM 模型。

    唯一约束：(asset_id, user_id)，确保同一用户对同一照片仅一条评分。
    评分值范围 1-5，由业务层校验。
    """

    __tablename__ = "photo_ratings"
    __table_args__ = (
        UniqueConstraint("asset_id", "user_id", name="uq_asset_user"),
    )

    id: Any = Column(Integer, primary_key=True, autoincrement=True)
    asset_id: Any = Column(String(128), nullable=False, index=True, comment="Immich 资产 ID")
    user_id: Any = Column(String(128), nullable=False, default="default", index=True, comment="用户标识")
    rating: Any = Column(Integer, nullable=False, comment="评分值 1-5")
    comment: Any = Column(String(1000), nullable=True, comment="评论文本")
    created_at: Any = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="创建时间",
    )
    updated_at: Any = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="更新时间",
    )


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(database_url: str) -> async_sessionmaker[AsyncSession]:
    """初始化异步数据库引擎并创建所有表。

    参数:
        database_url: SQLAlchemy 异步数据库 URL，如 sqlite+aiosqlite:///./data/ratings.db。

    返回:
        异步会话工厂，供依赖注入使用。
    """
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("数据库初始化完成: %s", database_url)
    return _session_factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取全局异步会话工厂。

    返回:
        已初始化的异步会话工厂。

    raises:
        RuntimeError: 数据库未初始化时抛出。
    """
    if _session_factory is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _session_factory


async def close_db() -> None:
    """关闭数据库引擎，释放连接池资源。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("数据库连接已关闭")
