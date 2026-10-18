"""API 集成测试：路由、校验错误与领域错误映射。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_search_service
from app.core.errors import UpstreamUnavailableError
from app.main import app
from app.models.response import SearchResponse, SearchResultItem


class FakeSearchService:
    def __init__(self):
        self.fail = False

    async def search(self, request):
        if self.fail:
            raise UpstreamUnavailableError("Immich 服务不可用")
        return SearchResponse(
            query=request.query,
            total=1,
            results=[
                SearchResultItem(
                    asset_id="1",
                    thumbnail_url="http://immich.test/api/assets/1/thumbnail?size=preview",
                    captured_at_source="exif",
                    similarity_score=0.8,
                    time_freshness=0.5,
                    final_score=0.71,
                )
            ],
        )


@pytest.fixture
def client():
    fake = FakeSearchService()
    app.dependency_overrides[get_search_service] = lambda: fake
    with TestClient(app) as c:
        c._fake = fake
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_smart_search_success(client):
    resp = client.post(
        "/api/v1/search/smart",
        json={"query": "海边日落", "asset_ids": ["asset-001"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["results"][0]["asset_id"] == "1"
    assert "x-request-id" in resp.headers


def test_validation_error_returns_400_unified_structure(client):
    resp = client.post(
        "/api/v1/search/smart",
        json={"query": "   ", "asset_ids": []},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAMS"


def test_upstream_unavailable_returns_502(client):
    client._fake.fail = True
    resp = client.post(
        "/api/v1/search/smart",
        json={"query": "海边", "asset_ids": ["asset-001"]},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "UPSTREAM_UNAVAILABLE"


def test_method_not_allowed(client):
    resp = client.get("/api/v1/search/smart")
    assert resp.status_code == 405
