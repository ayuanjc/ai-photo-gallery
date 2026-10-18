"""场景标签写回服务测试（T7 白名单：source=analyzed 且置信度达标才写）。"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx

from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack
from app.services.scene_writeback import SceneWriteBack


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


def _settings_with_writeback(settings):
    return settings.model_copy(update={"writeback_enabled": True})


def _ok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/tags" and request.method == "GET":
        return httpx.Response(200, json=[])
    if path == "/api/tags" and request.method == "POST":
        body = json.loads(request.content)
        return httpx.Response(201, json={"id": f"tag-{body['name']}", "name": body["name"]})
    if "/assets" in path and request.method == "PUT":
        return httpx.Response(200, json=[])
    return httpx.Response(404)


def _analyzed_scene(scenes=None, subs=None, confidence=None):
    return {
        "primary_scene": (scenes or [None])[0],
        "scenes": scenes or [],
        "sub_scenes": subs or [],
        "confidence": confidence or {},
        "source": "analyzed",
    }


async def test_writeback_scene_tags(settings, tmp_path):
    """analyzed 且置信度达标的场景标签被写回为 AI/<场景中文名>。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(_ok_handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["nature", "sunset"],
                    confidence={"nature": 0.9, "sunset": 0.8},
                ),
            },
            {
                "asset_id": "asset-002",
                "scene": _analyzed_scene(
                    scenes=["urban"], confidence={"urban": 0.7}
                ),
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 3
        assert all(e.success for e in entries)
        tag_names = {e.target for e in entries}
        assert all(name.startswith("AI·") for name in tag_names)
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
            {
                "asset_id": "asset-002",
                "scene": _analyzed_scene(scenes=[], confidence={}),
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 0
    finally:
        await client.aclose()


async def test_writeback_rejects_fallback_source(settings, tmp_path):
    """白名单守卫：source=fallback（兜底猜测）一律不写回。"""
    s = _settings_with_writeback(settings)
    calls: list[tuple] = []

    async def fake_add_tag(*args, **kwargs):
        calls.append(args)
        return None

    ic, client = make_client(lambda r: httpx.Response(404), s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        wb.add_tag = fake_add_tag
        swb = SceneWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": {
                    "primary_scene": "nature",
                    "scenes": ["nature"],
                    "confidence": {"nature": 0.95},
                    "source": "fallback",
                },
            },
            {
                "asset_id": "asset-002",
                "scene": {
                    "primary_scene": "city",
                    "scenes": ["city"],
                    "confidence": {"city": 0.95},
                    "source": "inferred",
                },
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert entries == []
        assert calls == []
    finally:
        await client.aclose()


async def test_writeback_rejects_low_confidence(settings, tmp_path):
    """白名单守卫：source=analyzed 但置信度低于 0.60 的标签不写。"""
    s = _settings_with_writeback(settings)
    calls: list[tuple] = []

    async def fake_add_tag(*args, **kwargs):
        calls.append(args)
        return None

    ic, client = make_client(lambda r: httpx.Response(404), s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        wb.add_tag = fake_add_tag
        swb = SceneWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["nature"], confidence={"nature": 0.59}
                ),
            },
            {
                "asset_id": "asset-002",
                "scene": _analyzed_scene(
                    scenes=["city"], confidence={}
                ),
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert entries == []
        assert calls == []
    finally:
        await client.aclose()


async def test_writeback_boundary_and_mixed_confidence(settings, tmp_path):
    """置信度恰为 0.60 写回；同一资产上高低置信混合时只写达标项。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(_ok_handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["nature", "city"],
                    confidence={"nature": 0.60, "city": 0.40},
                ),
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 1
        assert entries[0].target == "AI·自然"
    finally:
        await client.aclose()


async def test_writeback_sub_scene_tags(settings, tmp_path):
    """子场景标签写回为 AI/<子场景中文名>，与大场景标签并存。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(_ok_handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["animal"],
                    subs=["cat", "pet"],
                    confidence={"animal": 0.9, "cat": 0.85, "pet": 0.8},
                ),
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 3
        assert all(e.success for e in entries)
        tag_names = {e.target for e in entries}
        assert "AI·动物" in tag_names
        assert "AI·猫" in tag_names
        assert "AI·宠物" in tag_names
    finally:
        await client.aclose()


async def test_writeback_sub_scene_only(settings, tmp_path):
    """仅有子场景（无大场景）时也写回子场景标签。"""
    s = _settings_with_writeback(settings)
    ic, client = make_client(_ok_handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, s)

        results = [
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    subs=["night_view"], confidence={"night_view": 0.75}
                ),
            },
        ]
        entries = await swb.writeback_scene_tags(results)
        assert len(entries) == 1
        assert entries[0].target == "AI·城市夜色"
    finally:
        await client.aclose()


async def test_writeback_disabled(settings, tmp_path):
    """writeback_enabled=False 时不执行写回。"""
    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        wb = ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl")
        swb = SceneWriteBack(wb, settings)

        results = [
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["nature"], confidence={"nature": 0.9}
                ),
            },
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
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["nature"], confidence={"nature": 0.9}
                ),
            },
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
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["nature"], confidence={"nature": 0.9}
                ),
            },
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
            {
                "asset_id": "asset-001",
                "scene": _analyzed_scene(
                    scenes=["nature"], confidence={"nature": 0.9}
                ),
            },
        ]
        await swb.writeback_scene_tags(results)
        assert mock_wb.add_tag.called
        call_args = mock_wb.add_tag.call_args
        assert call_args[0][0] == "asset-001"
        assert call_args[0][1].startswith("AI·")
    finally:
        await client.aclose()
