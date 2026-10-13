"""批量导入集成测试：端到端流程（提取→上传）与 API 路由。"""
from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_batch_importer
from app.main import app
from app.models.batch_import import ImportRequest, ImportResponse, ImportResultItem
from app.services.immich_uploader import ImmichAssetUploader
from app.services.importer import BatchImporter
from app.services.metadata import MetadataExtractor


async def test_end_to_end_import(settings, tmp_path):
    """真实提取器 + MockTransport 上传器 + 编排服务端到端流程。"""
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"fake-image-content")
    mtime = 1700000000.0
    os.utime(str(f), (mtime, mtime))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "asset-1", "status": "created"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    uploader = ImmichAssetUploader(settings, client=client)
    extractor = MetadataExtractor(settings.default_datetime_iso)
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(ImportRequest(file_paths=[str(f)]))

    assert resp.total == 1
    assert resp.succeeded == 1
    assert resp.failed == 0
    assert resp.results[0].asset_id == "asset-1"
    assert resp.results[0].metadata is not None
    assert resp.results[0].metadata.taken_at_source == "file_modified"


async def test_end_to_end_upload_failure(settings, tmp_path):
    """上传返回 400 时逐项失败。"""
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"fake-image-content")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    uploader = ImmichAssetUploader(settings, client=client)
    extractor = MetadataExtractor(settings.default_datetime_iso)
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(ImportRequest(file_paths=[str(f)]))

    assert resp.succeeded == 0
    assert resp.failed == 1
    assert resp.results[0].asset_id is None


class FakeBatchImporter:
    """API 集成测试用编排服务替身。"""

    def __init__(self):
        self.calls: list[ImportRequest] = []

    async def import_files(self, request: ImportRequest) -> ImportResponse:
        self.calls.append(request)
        return ImportResponse(
            total=1,
            succeeded=1,
            failed=0,
            results=[
                ImportResultItem(file_path=request.file_paths[0], success=True, asset_id="asset-1")
            ],
        )


@pytest.fixture
def client():
    fake = FakeBatchImporter()
    app.dependency_overrides[get_batch_importer] = lambda: fake
    with TestClient(app) as c:
        c._fake = fake
        yield c
    app.dependency_overrides.clear()


def test_api_batch_import_success(client):
    """POST /api/v1/import/batch 成功返回。"""
    resp = client.post(
        "/api/v1/import/batch",
        json={"file_paths": ["/tmp/a.jpg"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["succeeded"] == 1
    assert body["results"][0]["asset_id"] == "asset-1"


def test_api_batch_import_validation_error(client):
    """空文件列表返回 400 统一错误结构。"""
    resp = client.post(
        "/api/v1/import/batch",
        json={"file_paths": []},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAMS"


def test_api_batch_import_dedup(client):
    """重复文件路径被去重。"""
    resp = client.post(
        "/api/v1/import/batch",
        json={"file_paths": ["/tmp/a.jpg", "/tmp/a.jpg"]},
    )
    assert resp.status_code == 200
    assert client._fake.calls[0].file_paths == ["/tmp/a.jpg"]