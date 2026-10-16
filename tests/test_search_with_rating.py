"""搜索附加评分功能集成测试。

验证搜索结果是否正确附加评分数据，以及 min_rating 过滤是否正常工作。
使用 mock SearchService 避免依赖真实 Immich 上游。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


from app.api.routes.ratings import router as ratings_router
from app.api.routes.search import router as search_router
from app.core.errors import AppError
from app.db import Base
from app.models.request import SearchRequest
from app.models.response import SearchResponse, SearchResultItem
from app.repositories.sql_rating import SqlRatingRepository
from app.services.rating_service import RatingService


class FakeSearchService:
    """模拟 SearchService，返回固定的搜索结果。"""

    def __init__(self, items: list[SearchResultItem]) -> None:
        self._items = items

    async def search(self, request: SearchRequest) -> SearchResponse:
        return SearchResponse(
            query=request.query,
            total=len(self._items),
            results=list(self._items),
        )


def _make_search_item(asset_id: str) -> SearchResultItem:
    """创建测试用搜索结果项。"""
    return SearchResultItem(
        asset_id=asset_id,
        thumbnail_url=f"http://test/thumb/{asset_id}",
        original_url=f"http://test/orig/{asset_id}",
        similarity_score=0.9,
        time_freshness=0.8,
        final_score=0.85,
    )


def _make_app(tmp_path: Path, items: list[SearchResultItem]) -> FastAPI:
    """创建带搜索路由和评分服务的测试应用。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_ratings.db'}"
    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    rating_service = RatingService(SqlRatingRepository(session_factory))

    app = FastAPI()
    app.state.rating_service = rating_service
    app.state.search_service = FakeSearchService(items)
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(ratings_router, prefix="/api/v1")

    @app.on_event("startup")
    async def _init_db() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code.value, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _val_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": {"code": "INVALID_PARAMS"}})

    return app


class TestSearchWithRating:
    """搜索附加评分测试。"""

    def test_ratings_attached_to_results(self, tmp_path: Path) -> None:
        """搜索结果应附加评分数据。"""
        items = [_make_search_item("a1"), _make_search_item("a2"), _make_search_item("a3")]
        app = _make_app(tmp_path, items)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})
            client.post("/api/v1/ratings", json={"asset_id": "a3", "rating": 2})

            resp = client.post("/api/v1/search/smart", json={
                "query": "test", "asset_ids": ["a1", "a2", "a3"]
            })
            assert resp.status_code == 200
            results = resp.json()["results"]
            ratings = {r["asset_id"]: r["rating"] for r in results}
            assert ratings["a1"] == 5
            assert ratings["a3"] == 2
            assert ratings["a2"] is None

    def test_min_rating_filter(self, tmp_path: Path) -> None:
        """min_rating 过滤应仅返回评分达标的资产。"""
        items = [_make_search_item("a1"), _make_search_item("a2"), _make_search_item("a3")]
        app = _make_app(tmp_path, items)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})
            client.post("/api/v1/ratings", json={"asset_id": "a2", "rating": 3})
            client.post("/api/v1/ratings", json={"asset_id": "a3", "rating": 1})

            resp = client.post("/api/v1/search/smart", json={
                "query": "test", "asset_ids": ["a1", "a2", "a3"], "min_rating": 4
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["results"][0]["asset_id"] == "a1"

    def test_min_rating_filters_unrated(self, tmp_path: Path) -> None:
        """min_rating 过滤应排除未评分的资产。"""
        items = [_make_search_item("a1"), _make_search_item("a2")]
        app = _make_app(tmp_path, items)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})

            resp = client.post("/api/v1/search/smart", json={
                "query": "test", "asset_ids": ["a1", "a2"], "min_rating": 3
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["results"][0]["asset_id"] == "a1"

    def test_no_rating_service_degrades_gracefully(self, tmp_path: Path) -> None:
        """评分服务不可用时搜索正常返回（降级）。"""
        items = [_make_search_item("a1")]
        app = _make_app(tmp_path, items)
        app.state.rating_service = None
        with TestClient(app) as client:
            resp = client.post("/api/v1/search/smart", json={
                "query": "test", "asset_ids": ["a1"]
            })
            assert resp.status_code == 200
            assert resp.json()["results"][0]["rating"] is None

    def test_search_without_min_rating(self, tmp_path: Path) -> None:
        """不传 min_rating 时不过滤，返回所有结果。"""
        items = [_make_search_item("a1"), _make_search_item("a2")]
        app = _make_app(tmp_path, items)
        with TestClient(app) as client:
            client.post("/api/v1/ratings", json={"asset_id": "a1", "rating": 5})

            resp = client.post("/api/v1/search/smart", json={
                "query": "test", "asset_ids": ["a1", "a2"]
            })
            assert resp.status_code == 200
            assert resp.json()["total"] == 2