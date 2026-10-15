"""写回管理服务测试：审计日志 + 一键回滚。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.immich import ImmichClient
from app.services.immich_writeback import ImmichWriteBack, WriteBackEntry
from app.services.writeback_manager import WriteBackManager


def make_client(handler, settings):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=settings.immich_base_url)
    return ImmichClient(settings, client=client), client


def _write_audit(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _settings_with_writeback(settings):
    return settings.model_copy(update={"writeback_enabled": True})


def test_read_audit_log(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    _write_audit(audit, [
        {"action": "add_tag", "asset_id": "a", "target": "AI/自然", "success": True, "timestamp": "2026-10-15T10:00:00Z"},
        {"action": "add_to_album", "asset_id": "a", "target": "album-1", "success": True, "timestamp": "2026-10-15T11:00:00Z"},
        {"action": "add_tag", "asset_id": "b", "target": "AI/城市", "success": False, "timestamp": "2026-10-15T12:00:00Z"},
    ])
    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        mgr = WriteBackManager(AsyncMock(), settings, audit_path=audit)
        entries = mgr.read_audit_log()
        assert len(entries) == 3
    finally:
        client.aclose() if False else None


def test_read_audit_log_filter_by_action(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    _write_audit(audit, [
        {"action": "add_tag", "asset_id": "a", "target": "AI/自然", "success": True, "timestamp": "t1"},
        {"action": "add_to_album", "asset_id": "a", "target": "album-1", "success": True, "timestamp": "t2"},
    ])
    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        mgr = WriteBackManager(AsyncMock(), settings, audit_path=audit)
        entries = mgr.read_audit_log(action="add_tag")
        assert len(entries) == 1
        assert entries[0]["action"] == "add_tag"
    finally:
        client.aclose() if False else None


def test_read_audit_log_filter_by_asset(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    _write_audit(audit, [
        {"action": "add_tag", "asset_id": "a", "target": "AI/自然", "success": True, "timestamp": "t1"},
        {"action": "add_tag", "asset_id": "b", "target": "AI/城市", "success": True, "timestamp": "t2"},
    ])
    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        mgr = WriteBackManager(AsyncMock(), settings, audit_path=audit)
        entries = mgr.read_audit_log(asset_id="b")
        assert len(entries) == 1
        assert entries[0]["asset_id"] == "b"
    finally:
        client.aclose() if False else None


def test_read_audit_log_empty(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    ic, client = make_client(lambda r: httpx.Response(404), settings)
    try:
        mgr = WriteBackManager(AsyncMock(), settings, audit_path=audit)
        entries = mgr.read_audit_log()
        assert entries == []
    finally:
        client.aclose() if False else None


async def test_rollback_recent(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    _write_audit(audit, [
        {"action": "add_tag", "asset_id": "a", "target": "AI/自然", "success": True, "timestamp": "t1"},
    ])
    s = _settings_with_writeback(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "tag-1", "value": "AI/自然"}])
        if "/assets" in request.url.path and request.method == "DELETE":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=audit)
        mgr = WriteBackManager(wb, s, audit_path=audit)
        result = await mgr.rollback_recent(count=10)
        assert result["requested"] == 1
        assert result["rolled_back"] == 1
        assert result["succeeded"] == 1
    finally:
        await client.aclose()


async def test_rollback_by_asset(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    _write_audit(audit, [
        {"action": "add_tag", "asset_id": "a", "target": "AI/自然", "success": True, "timestamp": "t1"},
        {"action": "add_tag", "asset_id": "b", "target": "AI/城市", "success": True, "timestamp": "t2"},
    ])
    s = _settings_with_writeback(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "tag-1", "value": "AI/自然"}])
        if "/assets" in request.url.path and request.method == "DELETE":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=audit)
        mgr = WriteBackManager(wb, s, audit_path=audit)
        result = await mgr.rollback_by_asset("a")
        assert result["asset_id"] == "a"
        assert result["requested"] == 1
    finally:
        await client.aclose()


async def test_detect_conflicts(settings, tmp_path):
    """检测资产已有的人工标签（非 AI 前缀）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/assets/asset-001":
            return httpx.Response(200, json={
                "id": "asset-001",
                "tags": [
                    {"id": "t1", "value": "AI/自然"},
                    {"id": "t2", "value": "旅行"},
                    {"id": "t3", "value": "AI/日落"},
                ],
            })
        return httpx.Response(404)

    s = _settings_with_writeback(settings)
    ic, client = make_client(handler, s)
    try:
        wb = ImmichWriteBack(ic, s, audit_path=tmp_path / "audit.jsonl")
        mgr = WriteBackManager(wb, s, audit_path=tmp_path / "audit.jsonl")
        conflicts = await mgr.detect_conflicts("asset-001")
        assert len(conflicts) == 1
        assert conflicts[0]["tag"] == "旅行"
        assert conflicts[0]["type"] == "human_tag"
        assert conflicts[0]["resolution"] == "skip"
    finally:
        await client.aclose()