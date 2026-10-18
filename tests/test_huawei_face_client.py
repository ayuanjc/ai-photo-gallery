"""华为云 FRS 人脸识别客户端测试（使用 httpx.MockTransport）。"""
from __future__ import annotations

import httpx
import pytest

from app.core.errors import VisionError, VisionTimeoutError, VisionUnavailableError
from app.services.vision.huawei_face import HuaweiFaceClient


def make_face_client(handler, face_settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return HuaweiFaceClient(face_settings, client=client), client


async def test_detect_faces_ok(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"faces": [{"bounding_box": {"width": 100, "top_left_x": 50, "top_left_y": 50, "height": 120}}]},
        )

    fc, client = make_face_client(handler, face_settings)
    try:
        faces = await fc.detect_faces("https://example.com/a.jpg")
        assert len(faces) == 1
        assert faces[0]["bounding_box"]["width"] == 100
    finally:
        await client.aclose()


async def test_detect_faces_empty(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"faces": []})

    fc, client = make_face_client(handler, face_settings)
    try:
        faces = await fc.detect_faces("https://example.com/a.jpg")
        assert faces == []
    finally:
        await client.aclose()


async def test_create_face_set_ok(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"face_set_name": "test-set"})

    fc, client = make_face_client(handler, face_settings)
    try:
        result = await fc.create_face_set("test-set", capacity=100)
        assert result["face_set_name"] == "test-set"
    finally:
        await client.aclose()


async def test_add_face_returns_face_id(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"face_id": "abc123"})

    fc, client = make_face_client(handler, face_settings)
    try:
        face_id = await fc.add_face_by_url("test-set", "https://example.com/a.jpg", "ext-1")
        assert face_id == "abc123"
    finally:
        await client.aclose()


async def test_add_face_no_face_id_raises(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    fc, client = make_face_client(handler, face_settings)
    try:
        with pytest.raises(VisionError):
            await fc.add_face_by_url("test-set", "https://example.com/a.jpg", "ext-1")
    finally:
        await client.aclose()


async def test_search_face_ok(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"faces": [{"face_id": "f2", "similarity": 0.95, "external_image_id": "ext-2"}]},
        )

    fc, client = make_face_client(handler, face_settings)
    try:
        results = await fc.search_face_by_id("test-set", "f1", top_n=10, threshold=0.6)
        assert len(results) == 1
        assert results[0]["similarity"] == 0.95
    finally:
        await client.aclose()


async def test_delete_face_set_ok(face_settings):
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        deleted.append(request.url.path)
        return httpx.Response(204)

    fc, client = make_face_client(handler, face_settings)
    try:
        await fc.delete_face_set("test-set")
        assert any("test-set" in p for p in deleted)
    finally:
        await client.aclose()


async def test_delete_face_set_not_found_no_raise(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    fc, client = make_face_client(handler, face_settings)
    try:
        await fc.delete_face_set("test-set")
    finally:
        await client.aclose()


async def test_auth_error_raises_vision_error(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    fc, client = make_face_client(handler, face_settings)
    try:
        with pytest.raises(VisionError) as exc_info:
            await fc.detect_faces("https://example.com/a.jpg")
        assert "鉴权" in exc_info.value.message
    finally:
        await client.aclose()


async def test_server_error_raises_unavailable(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    fc, client = make_face_client(handler, face_settings)
    try:
        with pytest.raises(VisionUnavailableError):
            await fc.detect_faces("https://example.com/a.jpg")
    finally:
        await client.aclose()


async def test_timeout_raises_vision_timeout(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    fc, client = make_face_client(handler, face_settings)
    try:
        with pytest.raises(VisionTimeoutError):
            await fc.detect_faces("https://example.com/a.jpg")
    finally:
        await client.aclose()


async def test_connect_error_raises_unavailable(face_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    fc, client = make_face_client(handler, face_settings)
    try:
        with pytest.raises(VisionUnavailableError):
            await fc.detect_faces("https://example.com/a.jpg")
    finally:
        await client.aclose()
