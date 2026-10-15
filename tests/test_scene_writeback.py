"""场景标签写回服务测试。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack
from app.services.scene_writeback import SceneWriteBack


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


def _settings_with_writeback(settings):
    return settings.model_copy(update={"writeback_enabled": True})


async def test_writeback_scene_tags(settings, tmp_path):
    """搜索结果中的场景标签被写回为 AI/<场景中文名>。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/api/tags" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": f"tag-{body['name']}", "name": body["name"]})
        if "/assets" in path and request.method == "PUT":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": {"primary_scene": "nature", "scenes": ["nature", "sunset"]},
            },
            {
                "asset_id": "asset-002",
                "scene": {"primary_scene": "urban", "scenes": ["urban"]},
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 3  # 2 scenes for asset-001 + 1 for asset-002
        assert all(e.success for e in entries)
        tag_names = {e.target for e in entries}
        assert all(name.startswith("AI/") for name in tag_names)
    finally:
        await client.aclose()


async def test_writeback_skips_no_scene(settings, tmp_path):
    """无场景标签的结果被跳过。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(lambda r: httpx.Response(404), s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {"asset_id": "asset-001", "scene": None},
            {"asset_id": "asset-002", "scene": {"primary_scene": None, "scenes": []}},
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 0
    finally:
        await client.aclose()


async def test_writeback_disabled(settings, tmp_path):
    """writeback_enabled=False 时不执行写回。"""
    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        wb = ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, settings)

        results = [
            {"asset_id": "asset-001", "scene": {"primary_scene": "nature", "scenes": ["nature"]}},
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 0
    finally:
        await client.aclose()


async def test_writeback_dry_run(settings, tmp_path):
    """dry_run=True 时不实际写入。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {"asset_id": "asset-001", "scene": {"primary_scene": "nature", "scenes": ["nature"]}},
        ]
        entries = await swb.writeback_scene_tags(results, dry_run=True)
        assert len(entries) == 1
        assert entries[0].dry_run is True
    finally:
        await client.aclose()


async def test_writeback_failure_non_blocking(settings, tmp_path):
    """写回失败不抛异常，返回失败条目。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {"asset_id": "asset-001", "scene": {"primary_scene": "nature", "scenes": ["nature"]}},
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 1
        assert entries[0].success is False
    finally:
        await client.aclose()


async def test_writeback_uses_mock(settings, tmp_path):
    """使用 mock 验证 writeback 被正确调用。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(lambda r: httpx.Response(404), s)
    try:
        mock_wb = AsyncMock()
        mock_wb.add_tag.return_value = type(
            "E", (), {"success": True, "dry_run": False, "target": "AI/自然", "action": "add_tag"}
        )()
        swb = SceneWriteBack(mock_wb, s)

        results = [
            {"asset_id": "asset-001", "scene": {"primary_scene": "nature", "scenes": ["nature"]}},
        ]
        await swb.writeback_scene_tags(results)
        assert mock_wb.add_tag.called
        call_args = mock_wb.add_tag.call_args
        assert call_args[0][0] == "asset-001"
        assert call_args[0][1].startswith("AI/")
    finally:
        await client.aclose()