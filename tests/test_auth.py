"""搜索接口 API Key 鉴权测试。"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.api.deps import get_search_service, get_settings_dep
from app.core.config import Settings
from app.main import app
from app.models.response import SearchResponse


class FakeSearchService:
    async def search(self, request):
        return SearchResponse(query=request.query, total=0, results=[])


@contextmanager
def make_client(search_api_key: str):
    """构造测试客户端：注入自定义 search_api_key 与假搜索服务。"""
    app.dependency_overrides[get_settings_dep] = lambda: Settings(
        search_api_key=search_api_key
    )
    app.dependency_overrides[get_search_service] = lambda: FakeSearchService()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _search(client: TestClient):
    return client.post(
        "/api/v1/search/smart",
        json={"query": "海边", "image_urls": ["https://example.com/a.jpg"]},
    )


def test_no_key_configured_allows_without_header():
    with make_client("") as c:
        resp = _search(c)
    assert resp.status_code == 200


def test_missing_key_returns_401():
    with make_client("secret-key") as c:
        resp = _search(c)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_wrong_key_returns_401():
    with make_client("secret-key") as c:
        resp = c.post(
            "/api/v1/search/smart",
            json={"query": "海边", "image_urls": ["https://example.com/a.jpg"]},
            headers={"x-api-key": "wrong-key"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_correct_key_returns_200():
    with make_client("secret-key") as c:
        resp = c.post(
            "/api/v1/search/smart",
            json={"query": "海边", "image_urls": ["https://example.com/a.jpg"]},
            headers={"x-api-key": "secret-key"},
        )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_health_no_key_required():
    with make_client("secret-key") as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_scene_dict_no_key_required():
    with make_client("secret-key") as c:
        resp = c.get("/api/v1/scene/dict")
    assert resp.status_code == 200
    body = resp.json()
    assert "scenes" in body
    assert "visual_attrs" in body