"""ImmichWriteBack 统一写回客户端测试（使用 httpx.MockTransport）。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack, WriteBackEntry


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


def _settings_with_writeback(settings):
    return settings.model_copy(update={"writeback_enabled": True})


async def test_add_tag_creates_and_assigns(settings, tmp_path):
    """标签不存在时：创建标签 → 添加资产到标签。"""
    tag_created = {"created": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/api/tags" and request.method == "POST":
            body = json.loads(request.content)
            tag_created["created"] = body["name"]
            return httpx.Response(201, json={"id": "tag-1", "name": body["name"]})
        if path == "/api/tags/tag-1/assets" and request.method == "PUT":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.add_tag("asset-001", "AI/自然")
        assert entry.success is True
        assert entry.target == "AI/自然"
        assert entry.target_id == "tag-1"
        assert tag_created["created"] == "AI/自然"
    finally:
        await client.aclose()


async def test_add_tag_idempotent_existing_tag(settings, tmp_path):
    """标签已存在时：跳过创建，直接添加资产。"""
    create_called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "tag-1", "value": "AI/自然"}])
        if path == "/api/tags" and request.method == "POST":
            create_called["count"] += 1
            return httpx.Response(201, json={"id": "tag-1", "name": "AI/自然"})
        if path == "/api/tags/tag-1/assets" and request.method == "PUT":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.add_tag("asset-001", "AI/自然")
        assert entry.success is True
        assert create_called["count"] == 0
    finally:
        await client.aclose()


async def test_add_tag_dry_run(settings, tmp_path):
    """dry_run=True 时：不实际写入，仅审计。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.add_tag("asset-001", "AI/自然", dry_run=True)
        assert entry.success is True
        assert entry.dry_run is True
    finally:
        await client.aclose()


async def test_add_tag_disabled(settings, tmp_path):
    """writeback_enabled=False 时：返回失败条目。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        wb = ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.add_tag("asset-001", "AI/自然")
        assert entry.success is False
        assert "未启用" in (entry.error or "")
    finally:
        await client.aclose()


async def test_get_or_create_album_existing(settings, tmp_path):
    """相册已存在时：返回已有 album_id。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/albums" and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "album-1", "albumName": "AI·自然·2026-10"}]
            )
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        album_id, entry = await wb.get_or_create_album("AI·自然·2026-10")
        assert album_id == "album-1"
        assert entry.success is True
    finally:
        await client.aclose()


async def test_get_or_create_album_new(settings, tmp_path):
    """相册不存在时：创建新相册。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/albums" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/albums" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": "album-new", "albumName": body["albumName"]})
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        album_id, entry = await wb.get_or_create_album("AI·城市·2026-10")
        assert album_id == "album-new"
        assert entry.success is True
    finally:
        await client.aclose()


async def test_add_to_album(settings, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/albums/album-1/assets" and request.method == "PUT":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.add_to_album("asset-001", "album-1")
        assert entry.success is True
    finally:
        await client.aclose()


async def test_rollback_reverses_add_tag(settings, tmp_path):
    """rollback 对 add_tag 执行 remove_tag。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "tag-1", "value": "AI/自然"}])
        if path == "/api/tags/tag-1/assets" and request.method == "PUT":
            return httpx.Response(200, json=[])
        if path == "/api/tags/tag-1/assets" and request.method == "DELETE":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        add_entry = await wb.add_tag("asset-001", "AI/自然")
        assert add_entry.success is True

        rollback_results = await wb.rollback([add_entry])
        assert len(rollback_results) == 1
        assert rollback_results[0].action == "remove_tag"
        assert rollback_results[0].success is True
    finally:
        await client.aclose()


async def test_rollback_skips_failed_and_dry_run(settings, tmp_path):
    """rollback 跳过失败和 dry_run 条目。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(lambda r: httpx.Response(404), s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entries = [
            WriteBackEntry(action="add_tag", asset_id="a", target="t", success=False),
            WriteBackEntry(action="add_tag", asset_id="b", target="t", success=True, dry_run=True),
        ]
        results = await wb.rollback(entries)
        assert len(results) == 0
    finally:
        await client.aclose()


async def test_audit_log_written(settings, tmp_path):
    """审计日志写入 JSONL 文件。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "tag-1", "value": "AI/自然"}])
        if request.url.path == "/api/tags/tag-1/assets" and request.method == "PUT":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    audit_path = tmp_path / "audit.jsonl"
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=audit_path)
        await wb.add_tag("asset-001", "AI/自然")
        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "add_tag"
        assert entry["asset_id"] == "asset-001"
        assert entry["target"] == "AI/自然"
        assert entry["success"] is True
    finally:
        await client.aclose()


async def test_failure_non_blocking(settings, tmp_path):
    """上游返回错误时：不抛异常，返回失败条目。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.add_tag("asset-001", "AI/自然")
        assert entry.success is False
        assert entry.error is not None
    finally:
        await client.aclose()