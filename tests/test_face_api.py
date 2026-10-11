"""人脸分类 API 集成测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_face_service
from app.core.errors import VisionError
from app.main import app
from app.models.face import FaceClassifyResponse, FaceGroup


class FakeFaceService:
    def __init__(self):
        self.fail = False

    async def classify(self, request):
        if self.fail:
            raise VisionError("FRS error")
        return FaceClassifyResponse(
            total_images=1,
            total_faces=1,
            group_count=1,
            groups=[
                FaceGroup(
                    group_id=1,
                    face_count=1,
                    image_count=1,
                    representative_image_url="https://example.com/a.jpg",
                    image_urls=["https://example.com/a.jpg"],
                )
            ],
        )


@pytest.fixture
def client():
    fake = FakeFaceService()
    app.dependency_overrides[get_face_service] = lambda: fake
    with TestClient(app) as c:
        c._fake = fake
        yield c
    app.dependency_overrides.clear()


def test_classify_success(client):
    resp = client.post(
        "/api/v1/faces/classify",
        json={"image_urls": ["https://example.com/a.jpg"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_count"] == 1
    assert body["groups"][0]["representative_image_url"] == "https://example.com/a.jpg"


def test_classify_validation_error_empty_urls(client):
    resp = client.post(
        "/api/v1/faces/classify",
        json={"image_urls": []},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAMS"


def test_classify_validation_error_bad_threshold(client):
    resp = client.post(
        "/api/v1/faces/classify",
        json={"image_urls": ["https://example.com/a.jpg"], "similarity_threshold": 1.5},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAMS"


def test_classify_vision_error_returns_502(client):
    client._fake.fail = True
    resp = client.post(
        "/api/v1/faces/classify",
        json={"image_urls": ["https://example.com/a.jpg"]},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "VISION_ERROR"


def test_classify_disabled_returns_502():
    saved = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/faces/classify",
                json={"image_urls": ["https://example.com/a.jpg"]},
            )
            assert resp.status_code == 502
            assert resp.json()["error"]["code"] == "VISION_ERROR"
    finally:
        app.dependency_overrides.update(saved)