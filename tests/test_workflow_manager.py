"""工作流管理服务测试。"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.immich import ImmichClient
from app.services.workflow_manager import WorkflowManager


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


async def test_list_workflows(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workflows":
            return httpx.Response(200, json=[{"id": "wf-1", "name": "test"}])
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        mgr = WorkflowManager(ic, settings)
        wfs = await mgr.list_workflows()
        assert len(wfs) == 1
        assert wfs[0]["name"] == "test"
    finally:
        await client.aclose()


async def test_create_workflow_with_steps(settings):
    created = {"called": False, "updated": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/workflows" and request.method == "POST":
            created["called"] = True
            return httpx.Response(201, json={"id": "wf-1", "name": "test", "steps": []})
        if path == "/api/workflows/wf-1" and request.method == "PUT":
            created["updated"] = True
            body = json.loads(request.content)
            return httpx.Response(200, json={"id": "wf-1", **body})
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        mgr = WorkflowManager(ic, settings)
        wf = await mgr.create_workflow(
            name="test",
            trigger="AssetCreate",
            description="test workflow",
            steps=[WorkflowManager.make_add_tags_step(["AI/自然"])],
        )
        assert wf is not None
        assert created["called"] is True
        assert created["updated"] is True
        assert len(wf.get("steps", [])) == 1
    finally:
        await client.aclose()


async def test_delete_workflow(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workflows/wf-1" and request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        mgr = WorkflowManager(ic, settings)
        success = await mgr.delete_workflow("wf-1")
        assert success is True
    finally:
        await client.aclose()


async def test_make_webhook_step():
    step = WorkflowManager.make_webhook_step(
        "http://localhost:8000/api/v1/plugin/hook",
        "POST",
        "x-plugin-token",
        "secret",
    )
    assert step["method"] == "immich-plugin-core#webhook"
    assert step["config"]["url"] == "http://localhost:8000/api/v1/plugin/hook"
    assert step["config"]["method"] == "POST"
    assert step["config"]["headerName"] == "x-plugin-token"
    assert step["config"]["headerValue"] == "secret"
    assert step["enabled"] is True


async def test_make_add_tags_step():
    step = WorkflowManager.make_add_tags_step(["AI/自然", "AI/日落"])
    assert step["method"] == "immich-plugin-core#assetAddTags"
    assert step["config"]["tags"] == ["AI/自然", "AI/日落"]


async def test_make_add_to_album_step():
    step = WorkflowManager.make_add_to_album_step("AI·自然·2026-10")
    assert step["method"] == "immich-plugin-core#assetAddToAlbums"
    assert step["config"]["albumName"] == "AI·自然·2026-10"
    assert step["config"]["albumIds"] == []


async def test_setup_default_workflows(settings):
    created_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workflows" and request.method == "POST":
            created_count["n"] += 1
            return httpx.Response(201, json={"id": f"wf-{created_count['n']}", "steps": []})
        if "/api/workflows/wf-" in request.url.path and request.method == "PUT":
            body = json.loads(request.content)
            return httpx.Response(200, json={"id": "wf-x", **body})
        return httpx.Response(404)

    ic, client = make_client(handler, settings)
    try:
        mgr = WorkflowManager(ic, settings)
        results = await mgr.setup_default_workflows(
            "http://localhost:8000/api/v1/plugin/hook",
            "secret-token",
        )
        assert len(results) == 3
        assert created_count["n"] == 3
    finally:
        await client.aclose()


async def test_failure_non_blocking(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    ic, client = make_client(handler, settings)
    try:
        mgr = WorkflowManager(ic, settings)
        wfs = await mgr.list_workflows()
        assert wfs == []
        wf = await mgr.create_workflow("test", "AssetCreate")
        assert wf is None
    finally:
        await client.aclose()