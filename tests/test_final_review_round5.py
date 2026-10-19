"""最终审查第五轮修复验证（fix/final-review-round5）。

覆盖：
- R5-1 评分路由鉴权：配置 search_api_key 后无/错 key 一律 401（公网裸奔关闭）
- R5-2 回填进度 errors 列表容量上限（防上游持续故障时 20MB 快照膨胀）
- R5-3 webhook asset.id 格式白名单（防路径注入/日志注入）
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services.backfill_service import BackfillService, _MAX_PROGRESS_ERRORS


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ---- R5-1：评分路由必须鉴权 ----


def _require_api_key(monkeypatch, key: str = "round5-test-key") -> None:
    """让 deps.verify_api_key 读到非空 search_api_key（auth 强制开启）。"""
    monkeypatch.setattr(
        "app.api.deps.get_settings",
        lambda: get_settings().model_copy(update={"search_api_key": key}),
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/ratings/stats"),
        ("POST", "/api/v1/ratings"),
        ("DELETE", "/api/v1/ratings/asset-x"),
        ("GET", "/api/v1/ratings/asset-x"),
        ("POST", "/api/v1/ratings/batch"),
        ("PUT", "/api/v1/ratings/asset-x"),
    ],
)
def test_r5_1_ratings_endpoints_reject_missing_key(client, monkeypatch, method, path):
    """无 x-api-key 访问任一评分端点必须 401（修复前公网可直写数据库）。"""
    _require_api_key(monkeypatch)
    resp = client.request(method, path, json={} if method in ("POST", "PUT") else None)
    assert resp.status_code == 401, f"{method} {path} 无 key 不得放行: {resp.status_code}"


def test_r5_1_ratings_reject_wrong_key(client, monkeypatch):
    """错误 x-api-key 同样 401。"""
    _require_api_key(monkeypatch, key="correct-key")
    resp = client.get(
        "/api/v1/ratings/stats", headers={"x-api-key": f"wrong-{uuid.uuid4().hex}"}
    )
    assert resp.status_code == 401


def test_r5_1_ratings_accepts_valid_key(client, monkeypatch):
    """正确 key 通过鉴权（放行到业务层）。"""
    _require_api_key(monkeypatch, key="correct-key")
    from app.models.rating import RatingStatsResponse

    fake_service = SimpleNamespace(
        get_stats=AsyncMock(
            return_value=RatingStatsResponse(total_rated=0, average_rating=0.0)
        )
    )
    monkeypatch.setattr(app.state, "rating_service", fake_service, raising=False)
    resp = client.get("/api/v1/ratings/stats", headers={"x-api-key": "correct-key"})
    assert resp.status_code == 200, resp.text


# ---- R5-2：回填 errors 上限 ----


def _make_backfill_service(page_size: int) -> BackfillService:
    settings = SimpleNamespace(backfill_page_size=page_size, backfill_concurrency=5)
    return BackfillService(
        immich=SimpleNamespace(),
        settings=settings,
        scene_analyzer=None,
        scene_writeback=None,
        redis_client=None,
    )


def test_r5_2_backfill_errors_capped():
    """单页 250 个资产全部失败：failed 计数精确，errors 列表封顶 _MAX_PROGRESS_ERRORS。"""
    svc = _make_backfill_service(page_size=250)
    assets = [{"id": f"asset-{i}"} for i in range(250)]

    async def fake_fetch_page(page, size, since):
        return assets if page == 1 else []

    async def fake_process_one(asset, sem, dry_run, skip_ids):
        raise RuntimeError("upstream down " + "x" * 300)

    svc._fetch_page = fake_fetch_page  # type: ignore[method-assign]
    svc._process_one = fake_process_one  # type: ignore[method-assign]

    asyncio.run(svc._do_backfill(limit=0, since="", dry_run=True, skip_tagged=False))

    assert svc._progress["failed"] == 250
    assert len(svc._progress["errors"]) == _MAX_PROGRESS_ERRORS
    # 单条错误文本仍截断 200 字符
    assert all(len(e) <= 200 for e in svc._progress["errors"])


def test_r5_2_backfill_fatal_exception_truncated():
    """整体异常路径（run_backfill except 分支）错误文本同样截断且不超上限。"""
    svc = _make_backfill_service(page_size=10)

    async def boom(limit, since, dry_run, skip_tagged):
        raise RuntimeError("fatal " + "y" * 500)

    svc._do_backfill = boom  # type: ignore[method-assign]

    result = asyncio.run(svc.run_backfill(limit=1))

    assert result["status"] == "failed"
    assert len(result["errors"]) == 1
    assert len(result["errors"][0]) <= 200


# ---- R5-3：webhook asset.id 格式白名单 ----


def _no_plugin_token(monkeypatch) -> None:
    """清空 plugin hook token（.env 中的真实 token 不得泄漏进测试）。"""
    monkeypatch.setattr(
        "app.api.routes.plugin.get_settings",
        lambda: get_settings().model_copy(update={"plugin_hook_token": ""}),
    )


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "abc\ndef",
        "asset id with space",
        "id;rm -rf /",
        "脚本<script>",
        "x" * 129,
    ],
)
def test_r5_3_plugin_rejects_malformed_asset_id(client, monkeypatch, bad_id):
    """含路径穿越/换行/空格/超长的 asset.id 必须被拒且不触发任何写回。"""
    _no_plugin_token(monkeypatch)
    analyze = AsyncMock()
    monkeypatch.setattr(
        app.state,
        "scene_analyzer",
        SimpleNamespace(analyze=analyze),
        raising=False,
    )
    scene_wb = AsyncMock()
    desc_wb = AsyncMock()
    album_wb = AsyncMock()
    monkeypatch.setattr(app.state, "scene_writeback", scene_wb, raising=False)
    monkeypatch.setattr(app.state, "description_writeback", desc_wb, raising=False)
    monkeypatch.setattr(app.state, "album_writeback", album_wb, raising=False)

    resp = client.post(
        "/api/v1/plugin/hook",
        json={"event": "AssetCreate", "asset": {"id": bad_id}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any("格式非法" in w for w in body.get("warnings", [])), body
    analyze.assert_not_awaited()
    scene_wb.writeback_scene_tags.assert_not_awaited()
    desc_wb.writeback_descriptions.assert_not_awaited()
    album_wb.writeback_albums.assert_not_awaited()


def test_r5_3_plugin_accepts_normal_asset_id(client, monkeypatch):
    """正常格式 asset.id 不受影响（继续走分析流程）。"""
    monkeypatch.setattr(
        "app.api.routes.plugin.get_settings",
        lambda: get_settings().model_copy(
            update={"plugin_hook_token": "", "writeback_enabled": True}
        ),
    )
    from app.scene.models import SceneTags

    analyze = AsyncMock(
        return_value=SceneTags(primary_scene=None, scenes=[], source="tag_inferred")
    )
    monkeypatch.setattr(
        app.state, "scene_analyzer", SimpleNamespace(analyze=analyze), raising=False
    )
    resp = client.post(
        "/api/v1/plugin/hook",
        json={"event": "AssetCreate", "asset": {"id": "normal-id_123-OK"}},
    )
    assert resp.status_code == 200
    assert not any("格式非法" in w for w in resp.json().get("warnings", []))
    analyze.assert_awaited_once()