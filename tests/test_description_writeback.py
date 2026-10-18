"""P6 描述/评分写回测试。"""
from __future__ import annotations

import json

import httpx

from app.services.description_writeback import DescriptionWriteBack
from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


def _settings_with_writeback(settings):
    return settings.model_copy(update={"writeback_enabled": True})


# ---- 描述生成 ----

def test_generate_description_with_scene(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    result = {
        "asset_id": "a1",
        "scene": {
            "primary_scene": "nature",
            "scenes": ["nature"],
            "sub_scenes": ["beach", "sunset"],
            "visual_attrs": {"time": "sunset", "people": "none"},
        },
    }
    desc = desc_wb._generate_description(result)
    assert "AI 分析" in desc
    assert "自然" in desc
    assert "海滩" in desc
    assert "日落" in desc


def test_generate_description_no_scene(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    desc = desc_wb._generate_description({"asset_id": "a1", "scene": None})
    assert desc == ""


def test_generate_description_default_scene_filtered(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    result = {
        "asset_id": "a1",
        "scene": {"primary_scene": "default", "scenes": [], "sub_scenes": []},
    }
    desc = desc_wb._generate_description(result)
    assert desc == ""


def test_generate_description_visual_attrs(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    result = {
        "asset_id": "a1",
        "scene": {
            "primary_scene": "city",
            "scenes": ["city"],
            "sub_scenes": [],
            "visual_attrs": {"time": "night", "indoor": "outdoor", "weather": "rainy"},
        },
    }
    desc = desc_wb._generate_description(result)
    assert "城市" in desc
    assert "夜晚" in desc
    assert "室外" in desc
    assert "雨天" in desc


# ---- 评分计算 ----

def test_calculate_rating_from_final_score(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    assert desc_wb._calculate_rating({"final_score": 0.9}) == 5
    assert desc_wb._calculate_rating({"final_score": 0.7}) == 4
    assert desc_wb._calculate_rating({"final_score": 0.5}) == 3
    assert desc_wb._calculate_rating({"final_score": 0.3}) == 2
    assert desc_wb._calculate_rating({"final_score": 0.1}) == 1


def test_calculate_rating_clamped(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    assert desc_wb._calculate_rating({"final_score": 1.5}) == 5
    assert desc_wb._calculate_rating({"final_score": -0.5}) == 1
    assert desc_wb._calculate_rating({"final_score": 0.0}) == 1


def test_calculate_rating_no_score(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    assert desc_wb._calculate_rating({}) == 0
    assert desc_wb._calculate_rating({"final_score": None}) == 0


def test_calculate_rating_fallback_to_similarity(settings):
    wb = ImmichWriteBack(ImmichClient(settings), settings)
    desc_wb = DescriptionWriteBack(wb, settings)
    assert desc_wb._calculate_rating({"similarity_score": 0.8}) == 4


# ---- 写回流程 ----

async def test_writeback_descriptions_disabled(settings, tmp_path):
    """写回未启用时返回空列表。"""
    ic = ImmichClient(settings)
    wb = ImmichWriteBack(ic, settings, audit_path=tmp_path / "audit.jsonl")
    desc_wb = DescriptionWriteBack(wb, settings)
    results = [{"asset_id": "a1", "scene": {"primary_scene": "nature"}, "final_score": 0.8}]
    entries = await desc_wb.writeback_descriptions(results)
    assert entries == []


async def test_writeback_descriptions_dry_run(settings, tmp_path):
    """dry_run 模式：仅审计不实际写入。"""
    s = _settings_with_writeback(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        desc_wb = DescriptionWriteBack(wb, s)
        results = [
            {
                "asset_id": "a1",
                "scene": {"primary_scene": "nature", "scenes": ["nature"]},
                "final_score": 0.8,
            }
        ]
        entries = await desc_wb.writeback_descriptions(results, dry_run=True)
        assert len(entries) == 2
        assert all(e.dry_run for e in entries)
        assert all(e.success for e in entries)
        assert entries[0].action == "update_description"
        assert entries[1].action == "update_rating"
    finally:
        await client.aclose()


async def test_writeback_descriptions_success(settings, tmp_path):
    """正常写回描述和评分。"""
    s = _settings_with_writeback(settings)
    updated = {"desc": False, "rating": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/assets/a1" and request.method == "GET":
            return httpx.Response(200, json={"id": "a1", "description": "", "rating": 0})
        if path == "/api/assets/a1" and request.method == "PUT":
            body = json.loads(request.content)
            if "description" in body:
                updated["desc"] = body["description"]
            if "rating" in body:
                updated["rating"] = body["rating"]
            return httpx.Response(200, json={"id": "a1"})
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        desc_wb = DescriptionWriteBack(wb, s)
        results = [
            {
                "asset_id": "a1",
                "scene": {
                    "primary_scene": "nature",
                    "scenes": ["nature"],
                    "sub_scenes": ["beach"],
                },
                "final_score": 0.8,
            }
        ]
        entries = await desc_wb.writeback_descriptions(results)
        assert len(entries) == 2
        assert entries[0].action == "update_description"
        assert entries[0].success is True
        assert "AI 分析" in updated["desc"]
        assert entries[1].action == "update_rating"
        assert entries[1].success is True
        assert updated["rating"] == 4
    finally:
        await client.aclose()


async def test_writeback_descriptions_api_error(settings, tmp_path):
    """Immich API 返回错误时，写回失败但不抛异常。"""
    s = _settings_with_writeback(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        desc_wb = DescriptionWriteBack(wb, s)
        results = [
            {
                "asset_id": "a1",
                "scene": {"primary_scene": "nature", "scenes": ["nature"]},
                "final_score": 0.8,
            }
        ]
        entries = await desc_wb.writeback_descriptions(results)
        assert all(not e.success for e in entries)
    finally:
        await client.aclose()


async def test_writeback_descriptions_no_scene(settings, tmp_path):
    """无场景标签时跳过描述写回，但仍写回评分。"""
    s = _settings_with_writeback(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/assets/a1" and request.method == "GET":
            return httpx.Response(200, json={"id": "a1", "rating": 0})
        if path == "/api/assets/a1" and request.method == "PUT":
            return httpx.Response(200, json={"id": "a1"})
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        desc_wb = DescriptionWriteBack(wb, s)
        results = [{"asset_id": "a1", "scene": None, "final_score": 0.6}]
        entries = await desc_wb.writeback_descriptions(results)
        assert len(entries) == 1
        assert entries[0].action == "update_rating"
    finally:
        await client.aclose()


# ---- ImmichWriteBack 新方法 ----

async def test_update_description_success(settings, tmp_path):
    s = _settings_with_writeback(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/assets/a1" and request.method == "GET":
            return httpx.Response(200, json={"description": "old desc"})
        if path == "/api/assets/a1" and request.method == "PUT":
            return httpx.Response(200, json={"id": "a1"})
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.update_description("a1", "new desc")
        assert entry.success is True
        assert entry.action == "update_description"
        assert entry.old_value == "old desc"
    finally:
        await client.aclose()


async def test_update_rating_clamped(settings, tmp_path):
    s = _settings_with_writeback(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/assets/a1" and request.method == "GET":
            return httpx.Response(200, json={"rating": 3})
        if path == "/api/assets/a1" and request.method == "PUT":
            return httpx.Response(200, json={"id": "a1"})
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        entry = await wb.update_rating("a1", 99)
        assert entry.success is True
        assert entry.target == "5"
        assert entry.old_value == "3"
    finally:
        await client.aclose()


async def test_rollback_description(settings, tmp_path):
    """回滚描述写回：恢复旧描述。"""
    s = _settings_with_writeback(settings)
    rollback_put = {"body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/assets/a1" and request.method == "GET":
            return httpx.Response(200, json={"description": "AI 分析：自然"})
        if path == "/api/assets/a1" and request.method == "PUT":
            rollback_put["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "a1"})
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        original = await wb.update_description("a1", "AI 分析：自然 / 海滩")
        assert original.success is True
        results = await wb.rollback([original])
        assert len(results) == 1
        assert results[0].success is True
        assert rollback_put["body"]["description"] == "AI 分析：自然"
    finally:
        await client.aclose()
