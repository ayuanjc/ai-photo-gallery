"""代码审查第三轮（fix/code-review-round3）回归测试。

覆盖：
- #1 get_watermark_processor 真实依赖可导入（原 from ..core.exceptions 必炸）
- #2 list_albums 不再关闭全局 Immich 客户端
- #3 settings 更新拒绝换行注入
- #5 plugin hook Redis SETNX 幂等去重（含降级）
- #7 asset_id 路径参数格式校验
- 低危：admin JSON 体校验 400 / hook_url 必填 / 审计坏行跳过 / 写回并发上限
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import _get_client_id, get_watermark_processor
from app.api.routes.plugin import _is_duplicate
from app.core.config import get_settings
from app.main import app
from app.services.immich_writeback import ImmichWriteBack
from app.services.writeback_manager import WriteBackManager


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---- #1 水印依赖真实导入（回归：原 app.core.exceptions 不存在必 500） ----


class _State:
    watermark_processor = object()


class _App:
    state = _State()


class _FakeRequest:
    app = _App()


def test_get_watermark_processor_real_dependency():
    """不使用 dependency_overrides，直接调用真实依赖函数。"""
    processor = get_watermark_processor(_FakeRequest())
    assert processor is not None


# ---- #2 list_albums 不关闭全局客户端 ----


def test_list_albums_keeps_global_client(client):
    """调用 /search/albums 后全局 Immich 客户端仍可用（未被 aclose）。"""
    resp = client.get("/api/v1/search/albums")
    assert resp.status_code == 200
    immich = app.state.immich_client
    client_after = getattr(immich, "_client", "missing")
    assert client_after is not None, "全局 Immich 客户端被 list_albums 关闭了"


# ---- #3 settings 换行注入 ----


def test_settings_update_rejects_newline_injection(client):
    """配置值包含换行符时拒绝（422），且 .env 文件未被修改。"""
    env_path = Path(".env")
    before = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    resp = client.put(
        "/admin/api/settings",
        json={"updates": {"immich_base_url": "http://x\nEVIL_INJECTED=1"}},
    )
    assert resp.status_code == 400

    resp = client.put(
        "/admin/api/settings",
        json={"updates": {"immich_base_url": "http://x\r\nEVIL_INJECTED=1"}},
    )
    assert resp.status_code == 400

    after = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    assert after == before, ".env 在请求被拒绝后仍然被写入了"


# ---- #5 plugin hook 幂等去重 ----


class _FakeRedis:
    def __init__(self):
        self._keys: set[str] = set()

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._keys:
            return None
        self._keys.add(key)
        return True


class _FakeRedisClient:
    def __init__(self, redis):
        self.redis = redis
        self.connected = redis is not None


class _RS:
    pass


class _RApp:
    def __init__(self, redis_client):
        s = _RS()
        s.redis_client = redis_client
        self.state = s


class _RRequest:
    def __init__(self, redis_client):
        self.app = _RApp(redis_client)


async def test_plugin_hook_dedup_same_event_and_updated_at():
    """同一事件 + 同一 updatedAt 重复投递时，第二次判定为重复。"""
    req = _RRequest(_FakeRedisClient(_FakeRedis()))
    asset = {"id": "asset-1", "updatedAt": "2026-01-01T00:00:00Z"}
    assert await _is_duplicate(req, "AssetCreate", asset) is False
    assert await _is_duplicate(req, "AssetCreate", asset) is True


async def test_plugin_hook_dedup_different_event_not_blocked():
    """不同事件（AssetCreate vs AssetMetadataExtraction）互不干扰。"""
    req = _RRequest(_FakeRedisClient(_FakeRedis()))
    asset = {"id": "asset-1", "updatedAt": "2026-01-01T00:00:00Z"}
    assert await _is_duplicate(req, "AssetCreate", asset) is False
    assert await _is_duplicate(req, "AssetMetadataExtraction", asset) is False


async def test_plugin_hook_dedup_updated_at_change_reprocesses():
    """资产 updatedAt 变化后允许重新触发（key 变化）。"""
    req = _RRequest(_FakeRedisClient(_FakeRedis()))
    v1 = {"id": "asset-1", "updatedAt": "2026-01-01T00:00:00Z"}
    v2 = {"id": "asset-1", "updatedAt": "2026-01-02T00:00:00Z"}
    assert await _is_duplicate(req, "AssetCreate", v1) is False
    assert await _is_duplicate(req, "AssetCreate", v1) is True
    assert await _is_duplicate(req, "AssetCreate", v2) is False


async def test_plugin_hook_dedup_degrades_without_redis():
    """Redis 不可用时放行（不阻塞主流程）。"""
    req = _RRequest(_FakeRedisClient(None))
    asset = {"id": "asset-1"}
    assert await _is_duplicate(req, "AssetCreate", asset) is False
    assert await _is_duplicate(req, "AssetCreate", asset) is False


# ---- #7 asset_id 路径校验 ----


@pytest.mark.parametrize("bad_id", ["bad.id", "bad%20id", "bad:id"])
def test_asset_id_pattern_rejected(client, bad_id):
    """非法 asset_id（含点/空格/冒号等）被 400 拒绝，不会拼进上游 URL。"""
    resp = client.get(f"/api/v1/assets/{bad_id}/thumbnail")
    assert resp.status_code == 400, f"asset_id={bad_id} 未被拒绝"
    assert resp.json()["error"]["code"] == "INVALID_PARAMS"


def test_asset_id_with_slash_not_routed(client):
    """含 / 的输入不命中单段路由（404），同样不会拼进上游 URL。"""
    resp = client.get("/api/v1/assets/bad/id/thumbnail")
    assert resp.status_code == 404


def test_asset_id_pattern_rejected_original(client):
    resp = client.get("/api/v1/assets/bad.id/original")
    assert resp.status_code == 400


# ---- 低危：admin JSON 体校验 / hook_url 必填 ----


def test_admin_rollback_invalid_json_returns_400(client):
    """非 JSON 请求体返回 400 而非 500。"""
    resp = client.post(
        "/admin/api/writeback/rollback",
        content=b"not-a-json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_admin_rollback_non_dict_body_returns_400(client):
    resp = client.post(
        "/admin/api/writeback/rollback",
        content=b"[1,2,3]",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_admin_rollback_invalid_count_returns_400(client):
    resp = client.post(
        "/admin/api/writeback/rollback",
        json={"count": -1},
    )
    assert resp.status_code == 400


def test_admin_workflows_setup_requires_hook_url(client):
    """未提供 hook_url 时返回 400，不再默认 127.0.0.1（容器内不可达）。"""
    resp = client.post("/admin/api/workflows/setup", json={})
    assert resp.status_code == 400


def test_admin_workflows_setup_rejects_bad_scheme(client):
    resp = client.post(
        "/admin/api/workflows/setup",
        json={"hook_url": "ftp://example.com/hook"},
    )
    assert resp.status_code == 400


# ---- 低危：审计日志坏行跳过 ----


def test_writeback_parse_entries_skips_bad_lines():
    good = {
        "action": "add_tag",
        "asset_id": "a1",
        "target": "AI/自然",
        "success": True,
    }
    bad_missing_target = {"action": "add_tag", "asset_id": "a2"}
    entries = WriteBackManager._parse_entries([good, bad_missing_target])
    assert len(entries) == 1
    assert entries[0].asset_id == "a1"


# ---- 低危：写回全局并发上限 ----


class _SlowFakeImmich:
    def __init__(self):
        self.concurrent = 0
        self.max_concurrent = 0

    async def request_json(self, method, path, **kwargs):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(0.02)
        self.concurrent -= 1
        return {"id": "x", "rating": 3, "description": ""}


async def test_writeback_semaphore_limits_concurrency(tmp_path, monkeypatch):
    """并发 20 个写回任务时，对 Immich 的瞬时并发不超过 8。"""
    fake = _SlowFakeImmich()
    wb = ImmichWriteBack(fake, get_settings(), audit_path=tmp_path / "audit.jsonl")
    monkeypatch.setattr(wb, "_enabled", lambda: True)

    results = await asyncio.gather(
        *(wb.update_rating(f"asset-{i}", 5) for i in range(20))
    )
    assert all(r.success for r in results)
    assert fake.max_concurrent <= 8, f"瞬时并发 {fake.max_concurrent} 超过上限"


# ---- 低危：限流 client_id 不落明文 API Key ----


def test_rate_limit_client_id_hashes_api_key():
    class _Client:
        host = "127.0.0.1"

    class _Req:
        headers = {"x-api-key": "sk-secret-value-123"}
        client = _Client()

    cid = _get_client_id(_Req(), get_settings())
    assert "sk-secret-value-123" not in cid
    assert cid.startswith("key:")


def test_rate_limit_client_id_uses_ip_without_key():
    class _Client:
        host = "10.0.0.7"

    class _Req:
        headers = {}
        client = _Client()

    cid = _get_client_id(_Req(), get_settings())
    assert cid == "ip:10.0.0.7"
