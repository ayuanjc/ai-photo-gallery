"""P29: 补充边缘测试 — admin 端点 + 边界场景。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.description_writeback import DescriptionWriteBack
from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack
from app.services.model_manager import ModelManager
from app.services.vector_index import VectorIndex

# ---- DescriptionWriteBack 边缘 ----

async def test_writeback_empty_results(settings, tmp_path):
    """空结果列表时返回空。"""
    s = settings.model_copy(update={"writeback_enabled": True})
    ic = ImmichClient(s)
    wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
    desc_wb = DescriptionWriteBack(wb, s)
    entries = await desc_wb.writeback_descriptions([])
    assert entries == []


async def test_writeback_no_asset_id(settings, tmp_path):
    """无 asset_id 时跳过。"""
    s = settings.model_copy(update={"writeback_enabled": True})
    ic = ImmichClient(s)
    wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
    desc_wb = DescriptionWriteBack(wb, s)
    results = [{"scene": {"primary_scene": "nature"}, "final_score": 0.8}]
    entries = await desc_wb.writeback_descriptions(results, dry_run=True)
    assert entries == []


async def test_writeback_zero_rating_skipped(settings, tmp_path):
    """评分为 0 时不写回评分。"""
    s = settings.model_copy(update={"writeback_enabled": True})
    ic = ImmichClient(s)
    wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
    desc_wb = DescriptionWriteBack(wb, s)
    results = [{"asset_id": "a1", "scene": {"primary_scene": "nature", "scenes": ["nature"]}}]
    entries = await desc_wb.writeback_descriptions(results, dry_run=True)
    assert len(entries) == 1
    assert entries[0].action == "update_description"


# ---- VectorIndex 边缘 ----

async def test_vector_index_empty_search():
    vi = VectorIndex()
    results = await vi.search([1.0, 0.0])
    assert results == []


async def test_vector_index_get_nonexistent():
    vi = VectorIndex()
    vec = await vi.get("nonexistent")
    assert vec is None


async def test_vector_index_remove_nonexistent():
    vi = VectorIndex()
    assert await vi.remove("nonexistent") is False


async def test_vector_index_search_top_k_larger_than_count():
    vi = VectorIndex()
    await vi.add("a1", [1.0, 0.0])
    await vi.add("a2", [0.0, 1.0])
    results = await vi.search([1.0, 0.0], top_k=100)
    assert len(results) == 2


# ---- ModelManager 边缘 ----

async def test_model_load_unregistered():
    mgr = ModelManager()
    with pytest.raises(KeyError):
        await mgr.load("nonexistent")


async def test_model_get_unregistered():
    mgr = ModelManager()
    with pytest.raises(KeyError):
        await mgr.get_model("nonexistent")


async def test_model_unload_not_loaded():
    mgr = ModelManager()
    async def loader():
        return "model"
    mgr.register("m1", loader)
    assert await mgr.unload("m1") is False


async def test_model_health_check_empty():
    mgr = ModelManager()
    health = await mgr.health_check()
    assert health == {}


# ---- ImmichWriteBack update_description/update_rating 边缘 ----

async def test_update_description_disabled(settings, tmp_path):
    """写回未启用时返回失败。"""
    ic = ImmichClient(settings)
    wb = ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl")
    entry = await wb.update_description("a1", "test")
    assert entry.success is False
    assert "未启用" in (entry.error or "")


async def test_update_rating_disabled(settings, tmp_path):
    """写回未启用时返回失败。"""
    ic = ImmichClient(settings)
    wb = ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl")
    entry = await wb.update_rating("a1", 3)
    assert entry.success is False
    assert "未启用" in (entry.error or "")


async def test_update_rating_zero_clamped(settings, tmp_path):
    """评分 0 被钳制为 0（允许 0 分）。"""
    s = settings.model_copy(update={"writeback_enabled": True})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/assets/a1" and request.method == "GET":
            return httpx.Response(200, json={"rating": 0})
        if path == "/api/assets/a1" and request.method == "PUT":
            return httpx.Response(200, json={"id": "a1"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=s.immich_base_url)
    ic = ImmichClient(s, client=client)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.update_rating("a1", -5)
        assert entry.success is True
        assert entry.target == "0"
    finally:
        await client.aclose()


async def test_rollback_rating(settings, tmp_path):
    """回滚评分写回：恢复旧评分。"""
    s = settings.model_copy(update={"writeback_enabled": True})
    rollback_body = {"body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/api/assets/a1" and request.method == "GET":
            return httpx.Response(200, json={"rating": 3})
        if path == "/api/assets/a1" and request.method == "PUT":
            rollback_body["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "a1"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=s.immich_base_url)
    ic = ImmichClient(s, client=client)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        original = await wb.update_rating("a1", 5)
        assert original.success is True
        assert original.old_value == "3"
        results = await wb.rollback([original])
        assert len(results) == 1
        assert results[0].success is True
        assert rollback_body["body"]["rating"] == 3
    finally:
        await client.aclose()
