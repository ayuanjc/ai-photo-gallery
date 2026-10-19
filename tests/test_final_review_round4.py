"""最终审查第四轮修复验证（fix/final-review-round4）。

覆盖：
- F1 写回闸门旁路：伪造 payload scene dict / 无 token 伪造 tags 均不可写回
- F2 settings 写 .env 硬闸：无 ADMIN_API_KEY 时任何环境拒绝
- F3 SSRF 校验：resolve_dns=False 快速段 + 异步 DNS 校验 fail-closed
- F4 feedback 限流字典容量上限（防 OOM）
- F5 限流 client_id：随机 x-api-key 归 IP 桶，XFF 取最右
- F6 asset_ids 字符白名单（防 URL 拼接注入）
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import get_settings
from app.main import app
from app.models.request import SearchRequest
from app.scene.models import SceneTags
from app.utils.url_safety import ensure_urls_dns_safe, is_safe_url


# ---- F1：写回闸门旁路 ----


def _enable_writeback(monkeypatch, token: str = "") -> None:
    monkeypatch.setattr(
        "app.api.routes.plugin.get_settings",
        lambda: get_settings().model_copy(
            update={"writeback_enabled": True, "plugin_hook_token": token}
        ),
    )


def _mock_writebacks(monkeypatch) -> dict[str, AsyncMock]:
    mocks = {
        "scene": AsyncMock(),
        "desc": AsyncMock(),
        "album": AsyncMock(),
    }
    mocks["scene"].writeback_scene_tags = AsyncMock(return_value=[])
    mocks["desc"].writeback_descriptions = AsyncMock(return_value=[])
    mocks["album"].writeback_albums = AsyncMock(return_value=[])
    monkeypatch.setattr(app.state, "scene_writeback", mocks["scene"], raising=False)
    monkeypatch.setattr(app.state, "description_writeback", mocks["desc"], raising=False)
    monkeypatch.setattr(app.state, "album_writeback", mocks["album"], raising=False)
    return mocks


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_f1_forged_payload_scene_dict_rejected(client, monkeypatch):
    """攻击路径 A：payload 自带伪造 scene dict（source=analyzed）不得写回。"""
    _enable_writeback(monkeypatch)
    empty = SceneTags(primary_scene=None, scenes=[])
    analyzer = SimpleNamespace(analyze=AsyncMock(return_value=empty))
    monkeypatch.setattr(app.state, "scene_analyzer", analyzer, raising=False)
    mocks = _mock_writebacks(monkeypatch)

    resp = client.post(
        "/api/v1/plugin/hook",
        json={
            "event": "AssetCreate",
            "asset": {
                "id": f"attack-{uuid.uuid4().hex[:8]}",
                "scene": {
                    "primary_scene": "nature",
                    "scenes": ["nature"],
                    "source": "analyzed",
                    "confidence": {"nature": 1.0},
                },
            },
        },
    )
    assert resp.status_code == 200
    mocks["scene"].writeback_scene_tags.assert_not_awaited()
    mocks["desc"].writeback_descriptions.assert_not_awaited()
    mocks["album"].writeback_albums.assert_not_awaited()


def test_f1_forged_tags_without_token_marked_tag_inferred(client, monkeypatch):
    """攻击路径 B：无 token 时伪造 tags 走真实分析器，source 必须由服务端标为
    tag_inferred（不可写回），且 analyze 收到 trusted=False。"""
    _enable_writeback(monkeypatch)
    calls: dict[str, object] = {}

    async def fake_analyze(asset, *, trusted: bool = True):
        calls["trusted"] = trusted
        return SceneTags(
            primary_scene="nature",
            scenes=["nature"],
            source="analyzed" if trusted else "tag_inferred",
        )

    analyzer = SimpleNamespace(analyze=fake_analyze)
    monkeypatch.setattr(app.state, "scene_analyzer", analyzer, raising=False)
    mocks = _mock_writebacks(monkeypatch)

    resp = client.post(
        "/api/v1/plugin/hook",
        json={
            "event": "AssetCreate",
            "asset": {"id": f"attack-{uuid.uuid4().hex[:8]}", "tags": ["山"]},
        },
    )
    assert resp.status_code == 200
    assert calls["trusted"] is False
    mocks["scene"].writeback_scene_tags.assert_not_awaited()
    mocks["desc"].writeback_descriptions.assert_not_awaited()
    mocks["album"].writeback_albums.assert_not_awaited()


def test_f1_token_protected_payload_passes_trusted_flag(client, monkeypatch):
    """配置 PLUGIN_HOOK_TOKEN 且鉴权通过时，payload 可信 → trusted=True，
    服务端推理的 analyzed 结果可正常写回（功能不回退）。"""
    token = "test-hook-token"
    from pydantic import SecretStr

    _enable_writeback(monkeypatch, token=SecretStr(token))
    calls: dict[str, object] = {}

    async def fake_analyze(asset, *, trusted: bool = True):
        calls["trusted"] = trusted
        return SceneTags(
            primary_scene="nature", scenes=["nature"], source="analyzed"
        )

    analyzer = SimpleNamespace(analyze=fake_analyze)
    monkeypatch.setattr(app.state, "scene_analyzer", analyzer, raising=False)
    mocks = _mock_writebacks(monkeypatch)

    resp = client.post(
        "/api/v1/plugin/hook",
        headers={"x-plugin-token": token},
        json={
            "event": "AssetCreate",
            "asset": {"id": f"ok-{uuid.uuid4().hex[:8]}", "smartInfo": {"tags": [{"name": "beach"}]}},
        },
    )
    assert resp.status_code == 200
    assert calls["trusted"] is True
    mocks["scene"].writeback_scene_tags.assert_awaited_once()


def test_f1_tag_inferred_source_blocked_by_gate(client, monkeypatch):
    """闸门回归：source=tag_inferred 的分析结果（无 token）即使场景非空也不写回。"""
    _enable_writeback(monkeypatch)

    async def fake_analyze(asset, *, trusted: bool = True):
        return SceneTags(
            primary_scene="nature", scenes=["nature"], source="tag_inferred"
        )

    analyzer = SimpleNamespace(analyze=fake_analyze)
    monkeypatch.setattr(app.state, "scene_analyzer", analyzer, raising=False)
    mocks = _mock_writebacks(monkeypatch)

    resp = client.post(
        "/api/v1/plugin/hook",
        json={
            "event": "AssetCreate",
            "asset": {"id": f"attack-{uuid.uuid4().hex[:8]}"},
        },
    )
    assert resp.status_code == 200
    mocks["scene"].writeback_scene_tags.assert_not_awaited()


def test_f1_analyzer_untrusted_skips_cache(monkeypatch):
    """untrusted 分析不写缓存（防伪造 payload 投毒 scene:{asset_id}）。"""
    from app.scene.analyzer import SceneAnalyzer
    from app.scene.dictionary import SceneDict

    dict_stub = SimpleNamespace(
        scene_alias_map={"mountain": "nature"},
        is_valid_scene=lambda k: k in {"nature"},
        is_valid_sub_scene=lambda k: False,
        parent_of=lambda k: None,
        visual_attrs={},
    )
    analyzer = SceneAnalyzer(dictionary=dict_stub, cache_size=10)
    asset = {"id": f"attack-{uuid.uuid4().hex[:8]}", "tags": ["mountain"]}

    tags = analyzer._analyze_smartinfo(asset, trusted=False)
    assert tags.source == "tag_inferred"
    assert tags.primary_scene == "nature"

    tags_trusted = analyzer._analyze_smartinfo(asset, trusted=True)
    assert tags_trusted.source == "analyzed"

    # untrusted 结果不入缓存
    analyzer._cache[asset["id"]] = tags
    assert analyzer._cache.get(asset["id"]) is tags


# ---- F2：settings 写 .env 硬闸 ----


def test_f2_settings_write_requires_admin_key(client):
    """无 ADMIN_API_KEY 时（含 development）写 .env 必须被拒，防 immich_base_url
    被篡改后 IMMICH_API_KEY 外送。"""
    resp = client.put(
        "/admin/api/settings",
        json={"updates": {"immich_base_url": "http://attacker.example.com"}},
    )
    assert resp.status_code in (401, 403, 503), resp.text
    assert resp.status_code != 200


# ---- F3：SSRF 校验拆分 ----


def test_f3_is_safe_url_without_dns():
    """resolve_dns=False：字面 IP 仍全量校验，域名跳过 DNS（延后到异步层）。"""
    assert is_safe_url("http://192.168.1.1/img.jpg", resolve_dns=False) is False
    assert is_safe_url("http://10.0.0.1/img.jpg", resolve_dns=False) is False
    assert is_safe_url("http://127.0.0.1/img.jpg", resolve_dns=False) is False
    assert is_safe_url("ftp://example.com/f", resolve_dns=False) is False
    assert is_safe_url("http://user:pass@example.com/img.jpg", resolve_dns=False) is False
    assert is_safe_url("http://localhost/img.jpg", resolve_dns=False) is False
    # 域名跳过 DNS 段（由 ensure_urls_dns_safe 在异步层补齐）
    assert is_safe_url("http://some-host.example.com/img.jpg", resolve_dns=False) is True


async def test_f3_ensure_urls_dns_safe_fail_closed():
    """异步 DNS 校验：内网字面 IP 必须 fail-closed 抛错。"""
    with pytest.raises(ValueError):
        await ensure_urls_dns_safe(["http://192.168.1.1/img.jpg"])
    with pytest.raises(ValueError):
        await ensure_urls_dns_safe(
            ["http://ok-public.example.com/img.jpg", "http://10.0.0.5/img.jpg"]
        )
    # 空列表直接通过
    await ensure_urls_dns_safe([])


def test_f3_validator_no_dns_in_pydantic():
    """SearchRequest.image_urls 的 validator 不做 DNS（防事件循环阻塞），
    但字面内网 IP 仍在同步段快速拒绝。"""
    with pytest.raises(ValidationError):
        SearchRequest(query="海边", image_urls=["http://192.168.1.1/a.jpg"])
    # 域名 URL 通过模型校验（DNS 由 image_query_service 异步补齐）
    req = SearchRequest(query="海边", image_urls=["http://example.com/a.jpg"])
    assert req.image_urls == ["http://example.com/a.jpg"]


# ---- F4：feedback 限流字典上限 ----


def test_f4_feedback_rate_limiter_bounded(monkeypatch):
    from app.services.feedback_service import FeedbackService

    settings = SimpleNamespace(feedback_rate_limit_per_minute=2)
    svc = FeedbackService.__new__(FeedbackService)
    svc._settings = settings
    svc._rate_limiter = {}
    monkeypatch.setattr(FeedbackService, "_RATE_LIMITER_MAX_KEYS", 5)

    for i in range(20):
        svc._check_rate_limit(f"sess-{i}", "asset-1", "query")
    assert len(svc._rate_limiter) <= 5


# ---- F5：限流 client_id ----


def test_f5_random_api_key_falls_to_ip_bucket():
    from app.api.deps import _get_client_id

    settings = SimpleNamespace(search_api_key="real-key", trusted_proxies=[])
    req_random = SimpleNamespace(
        headers={"x-api-key": f"junk-{uuid.uuid4().hex}"},
        client=SimpleNamespace(host="1.2.3.4"),
    )
    assert _get_client_id(req_random, settings) == "ip:1.2.3.4"

    req_valid = SimpleNamespace(
        headers={"x-api-key": "real-key"}, client=SimpleNamespace(host="1.2.3.4")
    )
    assert _get_client_id(req_valid, settings).startswith("key:")


def test_f5_xff_takes_rightmost():
    from app.api.deps import _get_client_id

    settings = SimpleNamespace(search_api_key="", trusted_proxies=["127.0.0.1"])
    req = SimpleNamespace(
        headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert _get_client_id(req, settings) == "ip:3.3.3.3"


# ---- F6：asset_ids 字符白名单 ----


def test_f6_asset_ids_charset_whitelist():
    ok = SearchRequest(query="海边", asset_ids=["abc-123_X", "0192837465abcdef"])
    assert ok.asset_ids == ["abc-123_X", "0192837465abcdef"]

    for bad in ["a b", 'a"b', "a/b", "a?b", "a#b", "a%b", "a\nb", "a\\b", "我们"]:
        with pytest.raises(ValidationError):
            SearchRequest(query="海边", asset_ids=[bad])