"""T6 清理污染数据测试：ImmichWriteBack.cleanup + POST /admin/api/immich/cleanup。"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack

TAGS = [
    {"id": "tag-1", "value": "AI·自然"},
    {"id": "tag-2", "value": "AI·城市"},
    {"id": "tag-3", "value": "旅行"},
]
ALBUMS = [
    {"id": "alb-1", "albumName": "AI·自然·2025-01", "assetCount": 3},
    {"id": "alb-2", "albumName": "🌐 云影图鉴 — AI 智能搜索", "assetCount": 12},
    {"id": "alb-3", "albumName": "我的家人相册", "assetCount": 5},
]


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/tags" and request.method == "GET":
        return httpx.Response(200, json=TAGS)
    if path == "/api/albums" and request.method == "GET":
        return httpx.Response(200, json=ALBUMS)
    if path.startswith("/api/tags/") and request.method == "DELETE":
        return httpx.Response(200, content=b"")
    if path.startswith("/api/albums/") and request.method == "DELETE":
        return httpx.Response(200, json={"count": 1})
    return httpx.Response(404)


def _make_wb(settings, tmp_path, handler=_handler):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=settings.immich_base_url,
    )
    ic = ImmichClient(settings, client=client)
    return ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl"), client


# ---- 服务级 ----


async def test_cleanup_dry_run_reports_without_delete(settings, tmp_path):
    """dry_run=True：返回将删除的清单，不发起任何 DELETE，不写审计。"""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return _handler(request)

    wb, client = _make_wb(settings, tmp_path, handler)
    try:
        report = await wb.cleanup(dry_run=True)
    finally:
        await client.aclose()

    assert report["dry_run"] is True
    assert [t["name"] for t in report["tags"]] == ["AI·自然", "AI·城市"]
    album_names = [a["name"] for a in report["albums"]]
    assert "AI·自然·2025-01" in album_names
    assert "🌐 云影图鉴 — AI 智能搜索" in album_names
    assert "我的家人相册" not in album_names
    assert report["deleted_tags"] == 0
    assert report["deleted_albums"] == 0
    assert report["failed"] == 0
    assert not any(m == "DELETE" for m, _ in calls)
    assert not (tmp_path / "audit.jsonl").exists()


async def test_cleanup_executes_and_audits(settings, tmp_path):
    """dry_run=False：逐项删除（含空响应体 DELETE）并逐条审计。

    settings 默认 writeback_enabled=False：清理是管理员显式操作，不受写回开关限制。
    """
    assert settings.writeback_enabled is False
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return _handler(request)

    wb, client = _make_wb(settings, tmp_path, handler)
    try:
        report = await wb.cleanup(dry_run=False)
    finally:
        await client.aclose()

    assert report["deleted_tags"] == 2
    assert report["deleted_albums"] == 2
    assert report["failed"] == 0
    assert "/api/tags/tag-1" in deleted
    assert "/api/tags/tag-2" in deleted
    assert "/api/albums/alb-1" in deleted
    assert "/api/albums/alb-2" in deleted
    assert "/api/tags/tag-3" not in deleted
    assert "/api/albums/alb-3" not in deleted

    audit_path = tmp_path / "audit.jsonl"
    assert audit_path.exists()
    entries = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {e["action"] for e in entries} == {"delete_tag", "delete_album"}
    assert all(e["success"] for e in entries)
    assert len(entries) == 4


async def test_cleanup_partial_failure_continues(settings, tmp_path):
    """单项删除失败计入 errors 并继续，不中断整体清理。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path == "/api/tags/tag-1":
            return httpx.Response(404)
        return _handler(request)

    wb, client = _make_wb(settings, tmp_path, handler)
    try:
        report = await wb.cleanup(dry_run=False)
    finally:
        await client.aclose()

    assert report["deleted_tags"] == 1
    assert report["deleted_albums"] == 2
    assert report["failed"] == 1
    assert len(report["errors"]) == 1


async def test_cleanup_handles_wrapped_response_shapes(settings, tmp_path):
    """Immich 响应形态兼容：{tags:[...]} / {albums:[...]} 包装对象。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"tags": TAGS})
        if request.url.path == "/api/albums":
            return httpx.Response(200, json={"albums": ALBUMS})
        return _handler(request)

    wb, client = _make_wb(settings, tmp_path, handler)
    try:
        report = await wb.cleanup(dry_run=True)
    finally:
        await client.aclose()

    assert len(report["tags"]) == 2
    assert len(report["albums"]) == 2


async def test_cleanup_scan_failure_degrades(settings, tmp_path):
    """标签/相册列表获取失败：降级为空清单 + errors，不抛异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/api/tags", "/api/albums"):
            return httpx.Response(500, json={"error": "boom"})
        return _handler(request)

    wb, client = _make_wb(settings, tmp_path, handler)
    try:
        report = await wb.cleanup(dry_run=True)
    finally:
        await client.aclose()

    assert report["tags"] == []
    assert report["albums"] == []
    assert len(report["errors"]) == 2


# ---- 路由级 ----


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class _FakeWriteBack:
    def __init__(self):
        self.calls: list[dict] = []

    async def cleanup(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "dry_run": kwargs["dry_run"],
            "tags": [{"id": "t", "name": "AI·自然"}],
            "albums": [],
            "deleted_tags": 0,
            "deleted_albums": 0,
            "failed": 0,
            "errors": [],
        }


def _with_writeback(fake):
    original = getattr(app.state, "immich_writeback", None)

    def restore():
        app.state.immich_writeback = original

    app.state.immich_writeback = fake
    return restore


def test_endpoint_defaults_to_dry_run(client):
    """不传 dry_run 时默认 True（安全默认，先看清单再删）。"""
    fake = _FakeWriteBack()
    restore = _with_writeback(fake)
    try:
        resp = client.post("/admin/api/immich/cleanup", json={})
    finally:
        restore()
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert fake.calls, "cleanup 未被调用"
    call = fake.calls[0]
    assert call["dry_run"] is True
    assert call["tag_prefix"] == "AI·"
    assert call["album_prefix"] == "AI·"
    assert "🌐 云影图鉴 — AI 智能搜索" in call["album_names"]


def test_endpoint_execute_passes_dry_run_false(client):
    fake = _FakeWriteBack()
    restore = _with_writeback(fake)
    try:
        resp = client.post("/admin/api/immich/cleanup", json={"dry_run": False})
    finally:
        restore()
    assert resp.status_code == 200
    assert fake.calls[0]["dry_run"] is False


def test_endpoint_rejects_non_bool_dry_run(client):
    resp = client.post("/admin/api/immich/cleanup", json={"dry_run": "yes"})
    assert resp.status_code == 400


def test_endpoint_rejects_empty_prefix(client):
    resp = client.post("/admin/api/immich/cleanup", json={"tag_prefix": ""})
    assert resp.status_code == 400
    resp = client.post("/admin/api/immich/cleanup", json={"album_prefix": "  "})
    assert resp.status_code == 400


def test_endpoint_rejects_bad_include_names(client):
    resp = client.post(
        "/admin/api/immich/cleanup", json={"include_album_names": [1, 2]}
    )
    assert resp.status_code == 400


def test_endpoint_uninitialized_returns_503(client):
    restore = _with_writeback(None)
    try:
        resp = client.post("/admin/api/immich/cleanup", json={})
    finally:
        restore()
    assert resp.status_code == 503
