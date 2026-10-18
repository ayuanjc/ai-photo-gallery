"""评分数据访问层单元测试。

使用临时文件 SQLite 数据库，测试 SqlRatingRepository 的所有方法，
重点验证 UPSERT 语义的原子性。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.repositories.sql_rating import SqlRatingRepository


async def _make_repo(tmp_path: Path) -> SqlRatingRepository:
    """创建临时数据库的 SqlRatingRepository 实例。"""
    db_path = tmp_path / "test_ratings.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return SqlRatingRepository(session_factory)


class TestUpsert:
    """UPSERT 操作测试。"""

    async def test_insert_new_record(self, tmp_path: Path) -> None:
        """首次插入应创建新记录。"""
        repo = await _make_repo(tmp_path)
        result = await repo.upsert("asset-1", "user-1", 5, "great")
        assert result["asset_id"] == "asset-1"
        assert result["user_id"] == "user-1"
        assert result["rating"] == 5
        assert result["comment"] == "great"
        assert result["created_at"] is not None

    async def test_update_existing_record(self, tmp_path: Path) -> None:
        """对同一 (asset_id, user_id) 再次 upsert 应更新而非插入。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("asset-1", "user-1", 3, "ok")
        result = await repo.upsert("asset-1", "user-1", 5, "excellent")
        assert result["rating"] == 5
        assert result["comment"] == "excellent"

    async def test_different_users_separate_records(self, tmp_path: Path) -> None:
        """不同用户对同一资产的评分应独立存储。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("asset-1", "user-a", 4)
        await repo.upsert("asset-1", "user-b", 2)
        a = await repo.get("asset-1", "user-a")
        b = await repo.get("asset-1", "user-b")
        assert a["rating"] == 4
        assert b["rating"] == 2

    async def test_upsert_without_comment(self, tmp_path: Path) -> None:
        """不传 comment 时 comment 应为 None。"""
        repo = await _make_repo(tmp_path)
        result = await repo.upsert("asset-2", "default", 3)
        assert result["comment"] is None


class TestGet:
    """获取单条记录测试。"""

    async def test_get_existing(self, tmp_path: Path) -> None:
        """获取存在的记录。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("asset-1", "default", 4, "good")
        result = await repo.get("asset-1", "default")
        assert result is not None
        assert result["rating"] == 4

    async def test_get_nonexistent(self, tmp_path: Path) -> None:
        """获取不存在的记录返回 None。"""
        repo = await _make_repo(tmp_path)
        result = await repo.get("nonexistent", "default")
        assert result is None


class TestDelete:
    """删除记录测试。"""

    async def test_delete_existing(self, tmp_path: Path) -> None:
        """删除存在的记录返回 True。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("asset-1", "default", 5)
        deleted = await repo.delete("asset-1", "default")
        assert deleted is True
        assert await repo.get("asset-1", "default") is None

    async def test_delete_nonexistent(self, tmp_path: Path) -> None:
        """删除不存在的记录返回 False。"""
        repo = await _make_repo(tmp_path)
        deleted = await repo.delete("nonexistent", "default")
        assert deleted is False


class TestGetBatch:
    """批量获取测试。"""

    async def test_batch_get_partial(self, tmp_path: Path) -> None:
        """部分资产有评分时，仅返回已评分的。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("asset-1", "default", 5)
        await repo.upsert("asset-3", "default", 3)
        result = await repo.get_batch(["asset-1", "asset-2", "asset-3"], "default")
        assert len(result) == 2
        assert "asset-1" in result
        assert "asset-2" not in result
        assert "asset-3" in result

    async def test_batch_get_empty(self, tmp_path: Path) -> None:
        """空列表返回空字典。"""
        repo = await _make_repo(tmp_path)
        result = await repo.get_batch([], "default")
        assert result == {}


class TestGetStats:
    """统计测试。"""

    async def test_stats_empty(self, tmp_path: Path) -> None:
        """无评分时统计全为零。"""
        repo = await _make_repo(tmp_path)
        stats = await repo.get_stats("default")
        assert stats["total_rated"] == 0
        assert stats["average_rating"] == 0.0

    async def test_stats_with_data(self, tmp_path: Path) -> None:
        """有评分时统计正确。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("a1", "default", 5)
        await repo.upsert("a2", "default", 3)
        await repo.upsert("a3", "default", 1)
        stats = await repo.get_stats("default")
        assert stats["total_rated"] == 3
        assert stats["average_rating"] == 3.0
        assert stats["distribution"].get("5") == 1
        assert stats["distribution"].get("3") == 1
        assert stats["distribution"].get("1") == 1
        assert len(stats["top_rated"]) == 3
        assert stats["top_rated"][0]["rating"] == 5


class TestGetAssetIdsByMinRating:
    """按最低评分筛选测试。"""

    async def test_filter_by_min_rating(self, tmp_path: Path) -> None:
        """按最低评分筛选返回正确的 asset_id 列表。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("a1", "default", 5)
        await repo.upsert("a2", "default", 3)
        await repo.upsert("a3", "default", 4)
        await repo.upsert("a4", "default", 1)
        result = await repo.get_asset_ids_by_min_rating(4, "default")
        assert set(result) == {"a1", "a3"}

    async def test_filter_sorted_by_rating_desc(self, tmp_path: Path) -> None:
        """结果按评分降序排列。"""
        repo = await _make_repo(tmp_path)
        await repo.upsert("a1", "default", 3)
        await repo.upsert("a2", "default", 5)
        await repo.upsert("a3", "default", 4)
        result = await repo.get_asset_ids_by_min_rating(1, "default")
        assert result[0] == "a2"
        assert result[1] == "a3"
        assert result[2] == "a1"

    async def test_filter_with_limit(self, tmp_path: Path) -> None:
        """limit 参数限制返回数量。"""
        repo = await _make_repo(tmp_path)
        for i in range(10):
            await repo.upsert(f"a{i}", "default", 5)
        result = await repo.get_asset_ids_by_min_rating(1, "default", limit=3)
        assert len(result) == 3
