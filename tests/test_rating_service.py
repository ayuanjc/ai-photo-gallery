"""评分业务服务层单元测试。

使用真实的 SqlRatingRepository + 临时数据库，测试 RatingService 的
业务逻辑与响应模型转换。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.repositories.sql_rating import SqlRatingRepository
from app.services.rating_service import RatingService


async def _make_service(tmp_path: Path) -> RatingService:
    """创建临时数据库的 RatingService 实例。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_ratings.db'}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return RatingService(SqlRatingRepository(session_factory))


class TestCreateOrUpdate:
    """创建/更新评分测试。"""

    async def test_create_new(self, tmp_path: Path) -> None:
        """创建新评分返回 RatingResponse。"""
        service = await _make_service(tmp_path)
        result = await service.create_or_update("a1", "default", 5, "great")
        assert result.asset_id == "a1"
        assert result.rating == 5
        assert result.comment == "great"

    async def test_update_existing(self, tmp_path: Path) -> None:
        """更新已有评分。"""
        service = await _make_service(tmp_path)
        await service.create_or_update("a1", "default", 3)
        result = await service.create_or_update("a1", "default", 5, "updated")
        assert result.rating == 5
        assert result.comment == "updated"


class TestGetRating:
    """获取单条评分测试。"""

    async def test_get_existing(self, tmp_path: Path) -> None:
        """获取存在的评分。"""
        service = await _make_service(tmp_path)
        await service.create_or_update("a1", "default", 4)
        result = await service.get_rating("a1", "default")
        assert result is not None
        assert result.rating == 4

    async def test_get_nonexistent(self, tmp_path: Path) -> None:
        """获取不存在的评分返回 None。"""
        service = await _make_service(tmp_path)
        result = await service.get_rating("nope", "default")
        assert result is None


class TestDeleteRating:
    """删除评分测试。"""

    async def test_delete_existing(self, tmp_path: Path) -> None:
        """删除存在的评分。"""
        service = await _make_service(tmp_path)
        await service.create_or_update("a1", "default", 5)
        result = await service.delete_rating("a1", "default")
        assert result.deleted is True

    async def test_delete_nonexistent(self, tmp_path: Path) -> None:
        """删除不存在的评分。"""
        service = await _make_service(tmp_path)
        result = await service.delete_rating("nope", "default")
        assert result.deleted is False


class TestGetBatchRatings:
    """批量获取评分测试。"""

    async def test_batch_partial(self, tmp_path: Path) -> None:
        """部分有评分时返回 ratings 和 missing_asset_ids。"""
        service = await _make_service(tmp_path)
        await service.create_or_update("a1", "default", 5)
        await service.create_or_update("a3", "default", 2)
        result = await service.get_batch_ratings(["a1", "a2", "a3"], "default")
        assert len(result.ratings) == 2
        assert result.missing_asset_ids == ["a2"]


class TestGetStats:
    """统计测试。"""

    async def test_stats_with_data(self, tmp_path: Path) -> None:
        """有数据时统计正确。"""
        service = await _make_service(tmp_path)
        await service.create_or_update("a1", "default", 5)
        await service.create_or_update("a2", "default", 3)
        stats = await service.get_stats("default")
        assert stats.total_rated == 2
        assert stats.average_rating == 4.0
        assert len(stats.top_rated) == 2


class TestGetRatingMap:
    """评分映射测试（供搜索附加评分使用）。"""

    async def test_rating_map(self, tmp_path: Path) -> None:
        """返回 {asset_id: rating} 映射。"""
        service = await _make_service(tmp_path)
        await service.create_or_update("a1", "default", 5)
        await service.create_or_update("a2", "default", 3)
        mapping = await service.get_rating_map(["a1", "a2", "a3"], "default")
        assert mapping == {"a1": 5, "a2": 3}
        assert "a3" not in mapping

    async def test_rating_map_empty(self, tmp_path: Path) -> None:
        """空列表返回空映射。"""
        service = await _make_service(tmp_path)
        mapping = await service.get_rating_map([], "default")
        assert mapping == {}


class TestGetAssetIdsByMinRating:
    """按最低评分筛选测试。"""

    async def test_filter(self, tmp_path: Path) -> None:
        """筛选符合最低评分的 asset_id。"""
        service = await _make_service(tmp_path)
        await service.create_or_update("a1", "default", 5)
        await service.create_or_update("a2", "default", 2)
        await service.create_or_update("a3", "default", 4)
        result = await service.get_asset_ids_by_min_rating(4, "default")
        assert set(result) == {"a1", "a3"}
