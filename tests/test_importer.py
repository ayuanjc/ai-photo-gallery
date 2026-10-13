"""批量导入编排服务测试（注入 Fake 提取器 / Fake 上传器）。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.errors import UpstreamError
from app.models.batch_import import ExifMetadata, ImportRequest
from app.services.importer import BatchImporter


class FakeExtractor:
    """可配置的元数据提取器替身。"""

    def __init__(self, raise_on: dict[str, Exception] | None = None):
        self.raise_on = raise_on or {}
        self.calls: list[str] = []

    def extract(self, file_path: str) -> ExifMetadata:
        self.calls.append(file_path)
        exc = self.raise_on.get(file_path)
        if exc is not None:
            raise exc
        return ExifMetadata(
            taken_at=datetime(2023, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
            taken_at_source="exif_datetime_original",
        )


class FakeUploader:
    """可配置的上传器替身。"""

    def __init__(self, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()
        self.uploads: list[dict] = []

    async def upload(self, file_path, *, device_id, taken_at, is_favorite=False):
        self.uploads.append(
            {
                "file_path": file_path,
                "device_id": device_id,
                "taken_at": taken_at,
                "is_favorite": is_favorite,
            }
        )
        if file_path in self.fail_on:
            raise UpstreamError("Immich 上传失败")
        return {"id": f"asset-{file_path}", "status": "created"}


def _request(file_paths, **overrides) -> ImportRequest:
    payload = {"file_paths": file_paths}
    payload.update(overrides)
    return ImportRequest(**payload)


async def test_import_success(settings):
    """全部文件导入成功。"""
    extractor = FakeExtractor()
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(_request(["a.jpg", "b.jpg"]))

    assert resp.total == 2
    assert resp.succeeded == 2
    assert resp.failed == 0
    assert all(r.success for r in resp.results)
    assert resp.results[0].asset_id == "asset-a.jpg"


async def test_import_file_not_found(settings):
    """文件不存在降级为逐项失败。"""
    extractor = FakeExtractor(raise_on={"a.jpg": FileNotFoundError("no such file")})
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(_request(["a.jpg"]))

    assert resp.total == 1
    assert resp.failed == 1
    assert resp.results[0].error == "文件不存在"


async def test_import_extract_error(settings):
    """元数据提取异常降级为逐项失败。"""
    extractor = FakeExtractor(raise_on={"a.jpg": RuntimeError("corrupt exif")})
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(_request(["a.jpg"]))

    assert resp.failed == 1
    assert "元数据提取失败" in resp.results[0].error


async def test_import_upload_error(settings):
    """上传失败降级为逐项失败，但仍保留元数据。"""
    extractor = FakeExtractor()
    uploader = FakeUploader(fail_on={"a.jpg"})
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(_request(["a.jpg"]))

    assert resp.failed == 1
    assert resp.results[0].error == "Immich 上传失败"
    assert resp.results[0].metadata is not None


async def test_import_partial_success(settings):
    """部分成功部分失败。"""
    extractor = FakeExtractor(raise_on={"b.jpg": FileNotFoundError("no such file")})
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(_request(["a.jpg", "b.jpg"]))

    assert resp.total == 2
    assert resp.succeeded == 1
    assert resp.failed == 1


async def test_import_device_id_override(settings):
    """请求级 device_id 覆盖默认值。"""
    extractor = FakeExtractor()
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    await importer.import_files(_request(["a.jpg"], device_id="custom-device"))

    assert uploader.uploads[0]["device_id"] == "custom-device"


async def test_import_default_device_id(settings):
    """未指定 device_id 时使用默认值。"""
    extractor = FakeExtractor()
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    await importer.import_files(_request(["a.jpg"]))

    assert uploader.uploads[0]["device_id"] == "batch-import"


async def test_import_is_favorite_passthrough(settings):
    """收藏标记透传到上传器。"""
    extractor = FakeExtractor()
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    await importer.import_files(_request(["a.jpg"], is_favorite=True))

    assert uploader.uploads[0]["is_favorite"] is True


async def test_import_dedup_file_paths(settings):
    """请求模型去重后仅处理一次。"""
    extractor = FakeExtractor()
    uploader = FakeUploader()
    importer = BatchImporter(settings, extractor, uploader)

    resp = await importer.import_files(_request(["a.jpg", "a.jpg"]))

    assert resp.total == 1
    assert len(extractor.calls) == 1