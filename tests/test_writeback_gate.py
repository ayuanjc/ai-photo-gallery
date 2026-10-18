"""T7 写回配置单源守卫：writeback_enabled 只认 .env / Settings。

回归背景：历史上 runtime_config.json 持有 {"writeback_enabled": true}，
与 .env 的 false 形成双源覆盖，导致用户以为写回关闭、实际却在写。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.runtime_config import FEATURES_META, RuntimeConfig
from app.main import app


def test_writeback_not_in_features_meta():
    """writeback_enabled 不得出现在运行时功能开关里。"""
    assert "writeback_enabled" not in FEATURES_META


def test_runtime_set_rejects_writeback_key():
    """运行时 set() 必须拒绝 writeback_enabled（KeyError）。"""
    import asyncio

    rc = RuntimeConfig(_rc_settings(), _rc_persist())
    with pytest.raises(KeyError):
        asyncio.run(rc.set("writeback_enabled", True))


def test_runtime_persist_ignores_stale_writeback_key(tmp_path):
    """持久化文件中残留的 writeback_enabled 键在加载时被忽略。

    autouse fixture 已把 RuntimeConfig 持久化路径 patch 到 tmp_path，
    直接往该路径写残留文件再构造即可。
    """
    persist = tmp_path / "runtime_config.json"
    persist.write_text(
        '{"writeback_enabled": true, "dedup_enabled": true}', encoding="utf-8"
    )
    rc = RuntimeConfig(_rc_settings(), persist)
    # 残留键不进入 overrides；合法键正常加载
    assert "writeback_enabled" not in rc._overrides
    assert rc._overrides.get("dedup_enabled") is True
    assert rc.is_enabled("writeback_enabled", False) is False
    assert rc.is_enabled("writeback_enabled", True) is True  # 只回退 Settings


def _rc_settings():
    from app.core.config import Settings

    return Settings(immich_api_key="test-key")


def _rc_persist():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp()) / "runtime_config.json"


def test_status_endpoint_reports_writeback_effective_value():
    """GET /admin/api/status 必须暴露写回实际生效值与来源说明。"""
    with TestClient(app) as client:
        resp = client.get("/admin/api/status")
    assert resp.status_code == 200
    data = resp.json()
    wb = data["writeback"]
    assert isinstance(wb["enabled"], bool)
    assert ".env" in wb["source"]
    assert wb["min_confidence"] == 0.60
    assert wb["dedup_ttl_seconds"] >= 1
