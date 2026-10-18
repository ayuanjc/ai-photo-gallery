"""AI 相册写回服务测试。"""
from __future__ import annotations

import json

import httpx

from app.services.album_writeback import AlbumWriteBack
from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


def _settings_with_writeback(settings):
    return settings.model_copy(update={"writeback_enabled": True})


async def test_writeback_albums_creates_and_adds(settings, tmp_path):
    """按场景+月份创建相册并添加资产。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/albums" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/api/albums" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": "album-1", "albumName": body["albumName"]})
        if "/assets" in path and request.method == "PUT":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        awb = AlbumWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": {"primary_scene": "nature", "scenes": ["nature"]},
                "captured_at": "2026-10-15T10:00:00Z",
            },
            {
                "asset_id": "asset-002",
                "scene": {"primary_scene": "nature", "scenes": ["nature"]},
                "captured_at": "2026-10-20T10:00:00Z",
            },
        ]
        entries = await awb.writeback_albums(results)
        assert len(entries) >= 3  # 1 create + 2 add
        assert any(e.action == "create_album" for e in entries)
        assert any(e.action == "add_to_album" for e in entries)
    finally:
        await client.aclose()


async def test_writeback_albums_existing_album(settings, tmp_path):
    """相册已存在时复用，不创建新相册。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/albums" and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "album-existing", "albumName": "AI·自然·2026-10"}]
            )
        if "/assets" in path and request.method == "PUT":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        awb = AlbumWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": {"primary_scene": "nature", "scenes": ["nature"]},
                "captured_at": "2026-10-15T10:00:00Z",
            },
        ]
        entries = await awb.writeback_albums(results)
        create_entries = [e for e in entries if e.action == "create_album"]
        assert len(create_entries) == 1
        assert create_entries[0].target_id == "album-existing"
    finally:
        await client.aclose()


async def test_writeback_albums_disabled(settings, tmp_path):
    """writeback_enabled=False 时不执行。"""
    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        wb = ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl")
        awb = AlbumWriteBack(wb, settings)

        results = [
            {"asset_id": "a", "scene": {"primary_scene": "nature", "scenes": ["nature"]}},
        ]
        entries = await awb.writeback_albums(results)
        assert len(entries) == 0
    finally:
        await client.aclose()


async def test_writeback_albums_no_scene(settings, tmp_path):
    """无场景标签的结果被跳过。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(lambda r: httpx.Response(404), s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        awb = AlbumWriteBack(wb, s)

        results = [
            {"asset_id": "a", "scene": None},
            {"asset_id": "b", "scene": {"primary_scene": None, "scenes": []}},
        ]
        entries = await awb.writeback_albums(results)
        assert len(entries) == 0
    finally:
        await client.aclose()


async def test_writeback_albums_dry_run(settings, tmp_path):
    """dry_run=True 时不实际写入。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/albums" and request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        awb = AlbumWriteBack(wb, s)

        results = [
            {
                "asset_id": "a",
                "scene": {"primary_scene": "nature", "scenes": ["nature"]},
                "captured_at": "2026-10-15T10:00:00Z",
            },
        ]
        entries = await awb.writeback_albums(results, dry_run=True)
        assert all(e.dry_run for e in entries)
    finally:
        await client.aclose()


async def test_writeback_albums_failure_non_blocking(settings, tmp_path):
    """上游失败时不抛异常。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        awb = AlbumWriteBack(wb, s)

        results = [
            {
                "asset_id": "a",
                "scene": {"primary_scene": "nature", "scenes": ["nature"]},
                "captured_at": "2026-10-15T10:00:00Z",
            },
        ]
        entries = await awb.writeback_albums(results)
        assert all(not e.success for e in entries if e.action == "create_album")
    finally:
        await client.aclose()
