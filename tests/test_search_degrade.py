"""搜索上游不可用降级测试。"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_search_service
from app.core.errors import UpstreamTimeoutError, UpstreamUnavailableError
from app.main import app
from app.models.request import SearchRequest
from app.services.search_service import SearchService
from app.services.vision.base import NullVisionProvider


class FakeImmich:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    async def smart_search(self, query: str, k: int = 50):
        if self._exc is not None:
            raise self._exc
        return []

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"http://immich.test/api/assets/{asset_id}/original"


def _request() -> SearchRequest:
    return SearchRequest(query="海滩", image_urls=["https://example.com/a.jpg"])


def _service(settings, exc: Exception | None) -> SearchService:
    return SearchService(settings, FakeImmich(exc), NullVisionProvider())


@contextmanager
def make_client(settings, exc: Exception | None = None, raise_server_exceptions: bool = True):
    app.dependency_overrides[get_search_service] = lambda: _service(settings, exc)
    with TestClient(app, raise_server_exceptions=raise_server_exceptions) as c:
        yield c
    app.dependency_overrides.clear()


def _body():
    return {"query": "海滩", "image_urls": ["https://example.com/a.jpg"]}


# ---- 单元：降级逻辑 ----

async def test_degrade_on_upstream_unavailable(settings):
    svc = _service(settings, UpstreamUnavailableError("immich down"))
    resp = await svc.search(_request())

    assert resp.degraded is True
    assert resp.total == 0
    assert resp.results == []
    assert resp.warnings == ["Immich 暂不可用，返回空结果"]


async def test_degrade_on_upstream_timeout(settings):
    svc = _service(settings, UpstreamTimeoutError("immich timeout"))
    resp = await svc.search(_request())

    assert resp.degraded is True
    assert resp.total == 0
    assert resp.results == []
    assert resp.warnings


async def test_internal_error_not_degraded(settings):
    svc = _service(settings, ValueError("internal bug"))
    with pytest.raises(ValueError):
        await svc.search(_request())


# ---- 集成：接口层面 ----

def test_api_returns_200_when_degraded(settings):
    with make_client(settings, UpstreamUnavailableError("immich down")) as c:
        resp = c.post("/api/v1/search/smart", json=_body())

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["total"] == 0
    assert body["results"] == []
    assert body["warnings"]


def test_api_returns_200_when_timeout(settings):
    with make_client(settings, UpstreamTimeoutError("immich timeout")) as c:
        resp = c.post("/api/v1/search/smart", json=_body())

    assert resp.status_code == 200
    assert resp.json()["degraded"] is True


def test_api_returns_500_on_internal_error(settings):
    with make_client(settings, ValueError("boom"), raise_server_exceptions=False) as c:
        resp = c.post("/api/v1/search/smart", json=_body())

    assert resp.status_code == 500


def test_api_returns_400_on_validation_error(settings):
    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/search/smart",
            json={"query": "", "image_urls": ["https://example.com/a.jpg"]},
        )

    assert resp.status_code == 400