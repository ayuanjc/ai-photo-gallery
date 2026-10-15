"""评分 API 路由层集成测试。

使用 FastAPI TestClient 测试 6 个 REST 端点，
验证请求处理、响应格式、错误码和 HTTP 状态码。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.ratings import router
from app.core.errors import AppError, ErrorCode
from app.db import Base
from app.repositories.sql_rating import SqlRatingRepository
from app.services.rating_service import RatingService


def _make_app(tmp_path: Path) -> FastAPI:
    """创建带评分路由和临时数据库的 FastAPI 测试应用。"""
    import asyncio

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_ratings.db'}"
    engine = create_async_engine(db_url, echo=False)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_init_tables(engine))
    loop.close()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    service = RatingService(SqlRatingRepository(session_factory))

    app = FastAPI()
    app.state.rating_service = service
    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code.value, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": {"code": "INVALID_PARAMS", "message": "参数校验失败"}})

    return app


async def _init_tables(engine) -> None:
    """异步创建数据库表。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class TestCreateRating:
    """POST /ratings 创建评分测试。"""

    def test_create_success(self, tmp_path: Path) -> None:
        """成功创建评分返回 200。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            resp = client.post("/api/v1/ratings", json={
                "asset_id": "a1", "rating": 5, "user_id": "default", "comment": "great"
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["asset_id"] == "a1"
            assert data["rating"] == 5

    def test_create_invalid_rating(self, tmp_path: Path) -> None:
        """评分超出 1-5 范围返回 400。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            resp = client.post("/api/v1/ratings", json={
                "asset_id": "a1", "rating": 6
            })
            assert resp.status_code == 400

    def test_create_upsert_semantics(self, tmp_path: Path) -> None:
        """重复创建同一资产评分应更新而非报错。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 3})
            resp = client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})
            assert resp.status_code == 200
            assert resp.json()["rating"] == 5


class TestUpdateRating:
    """PUT /ratings/{asset_id} 更新评分测试。"""

    def test_update_success(self, tmp_path: Path) -> None:
        """成功更新评分。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 3})
            resp = client.put("/api/v1/ratings/a1", json={"rating": 4, "comment": "better"})
            assert resp.status_code == 200
            assert resp.json()["rating"] == 4

    def test_update_creates_if_not_exists(self, tmp_path: Path) -> None:
        """更新不存在的评分应创建新记录。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            resp = client.put("/api/v1/ratings/new-asset", json={"rating": 5})
            assert resp.status_code == 200
            assert resp.json()["asset_id"] == "new-asset"


class TestGetRating:
    """GET /ratings/{asset_id} 获取评分测试。"""

    def test_get_existing(self, tmp_path: Path) -> None:
        """获取存在的评分。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 4})
            resp = client.get("/api/v1/ratings/a1")
            assert resp.status_code == 200
            assert resp.json()["rating"] == 4

    def test_get_nonexistent_returns_404(self, tmp_path: Path) -> None:
        """获取不存在的评分返回 404。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/v1/ratings/nope")
            assert resp.status_code == 404


class TestDeleteRating:
    """DELETE /ratings/{asset_id} 删除评分测试。"""

    def test_delete_existing(self, tmp_path: Path) -> None:
        """删除存在的评分。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})
            resp = client.delete("/api/v1/ratings/a1")
            assert resp.status_code == 200
            assert resp.json()["deleted"] is True

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        """删除不存在的评分返回 deleted=False。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            resp = client.delete("/api/v1/ratings/nope")
            assert resp.status_code == 200
            assert resp.json()["deleted"] is False


class TestBatchRatings:
    """POST /ratings/batch 批量获取测试。"""

    def test_batch_partial(self, tmp_path: Path) -> None:
        """部分有评分时返回 ratings 和 missing。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})
            client.post("/api/v1/ratings", json={"asset_id": "a3", "rating": 2})
            resp = client.post("/api/v1/ratings/batch", json={
                "asset_ids": ["a1", "a2", "a3"]
            })
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["ratings"]) == 2
            assert data["missing_asset_ids"] == ["a2"]


class TestRatingStats:
    """GET /ratings/stats 统计测试。"""

    def test_stats_with_data(self, tmp_path: Path) -> None:
        """有数据时统计正确。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})
            client.post("/api/v1/ratings", json={"asset_id": "a2", "rating": 3})
            resp = client.get("/api/v1/ratings/stats")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_rated"] == 2
            assert data["average_rating"] == 4.0

    def test_stats_empty(self, tmp_path: Path) -> None:
        """无数据时统计全为零。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/v1/ratings/stats")
            assert resp.status_code == 200
            assert resp.json()["total_rated"] == 0