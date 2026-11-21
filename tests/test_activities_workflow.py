"""活动台账 + 素材包工作流测试（台账锁定范围、池内编辑、导出）。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.models.activity import (
    PackageEditRequest,
    ResolveRequest,
    SearchInActivityRequest,
)
from app.models.request import SearchRequest
from app.models.response import SearchResponse, SearchResultItem
from app.services.package_store import PackageStore
from app.services.workflow_service import WorkflowService
from app.workflow import ActivityStore, apply_ops, build_package


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    doc = {
        "version": 1,
        "activities": [
            {
                "id": "act-a",
                "name": "迎新晚会",
                "aliases": ["2026 迎新", "迎新汇演"],
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T23:59:59Z",
                "organizer": "校团委",
                "venue": "大学生活动中心",
                "asset_ids": ["a1", "a2", "a3", "a4", "a5", "a6"],
                "cover_asset_id": "a1",
            },
            {
                "id": "act-b",
                "name": "志愿服务",
                "aliases": ["志愿活动"],
                "starts_at": "2025-08-10T00:00:00Z",
                "ends_at": "2025-08-12T23:59:59Z",
                "asset_ids": ["b1", "b2", "b3"],
            },
            {
                "id": "act-c",
                "name": "迎新晚会彩排",
                "aliases": ["迎新"],
                "starts_at": "2026-09-18T00:00:00Z",
                "ends_at": "2026-09-18T23:59:59Z",
                "asset_ids": ["c1"],
            },
        ],
    }
    path = tmp_path / "activities.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _candidates() -> list[dict]:
    return [
        {"asset_id": "a1", "score": 0.91, "w": 1920, "h": 1080, "taken_at": "2026-09-20T19:10:00Z", "scene": "舞台"},
        {"asset_id": "a2", "score": 0.88, "w": 1080, "h": 1920, "taken_at": "2026-09-20T19:12:00Z", "scene": "舞台"},
        {"asset_id": "a3", "score": 0.85, "w": 1200, "h": 1200, "taken_at": "2026-09-20T19:15:00Z", "scene": "观众"},
        {"asset_id": "a4", "score": 0.80, "w": 1920, "h": 1080, "taken_at": "2026-09-20T19:20:00Z"},
        {"asset_id": "a5", "score": 0.75, "w": 1080, "h": 1920, "taken_at": "2026-09-20T19:25:00Z"},
        {"asset_id": "a6", "score": 0.70, "w": 1920, "h": 1080, "taken_at": "2026-09-20T19:30:00Z"},
    ]


# ---- 台账解析 -----------------------------------------------------------

def test_resolve_exact_and_noise(ledger: Path) -> None:
    store = ActivityStore(ledger)
    hit = store.resolve(query="迎新晚会")
    assert hit.status == "matched"
    assert hit.activity is not None and hit.activity["id"] in {"act-a", "act-c"}

    noisy = store.resolve(query="志愿服务 推文封面")
    assert noisy.status == "matched"
    assert noisy.activity is not None and noisy.activity["id"] == "act-b"


def test_resolve_ambiguous_never_guesses(ledger: Path) -> None:
    store = ActivityStore(ledger)
    out = store.resolve(query="迎新")
    assert out.status == "ambiguous"
    assert out.need == "pick_one"
    ids = {c["id"] for c in out.candidates}
    assert {"act-a", "act-c"} <= ids


def test_resolve_by_date_and_miss(ledger: Path) -> None:
    store = ActivityStore(ledger)
    hit = store.resolve(date="2025-08-11")
    assert hit.status == "matched"
    assert hit.activity is not None and hit.activity["id"] == "act-b"

    assert store.resolve(date="1999-01-01").status == "none"


# ---- 素材包 -------------------------------------------------------------

def test_build_package_respects_orientation() -> None:
    activity = {"id": "act-a", "name": "迎新晚会", "starts_at": "2026-09-20T00:00:00Z"}
    state = build_package(activity, _candidates(), slots={"cover": 2, "body": 3}, orientation="landscape")
    cover = state["package"]["cover"]
    assert len(cover) == 2
    assert all(item["orientation"] == "landscape" for item in cover)
    assert "适配封面" in cover[0]["reason"]
    assert state["pool_size"] == 6
    assert state["revision"] == 1


def test_apply_ops_replace_then_stale_revision() -> None:
    activity = {"id": "act-a", "name": "迎新晚会", "starts_at": "2026-09-20T00:00:00Z"}
    state = build_package(activity, _candidates(), slots={"cover": 2, "body": 2})

    after = apply_ops(state, [{"op": "replace", "slot": "cover", "orientation": "portrait", "count": 2}], expected_revision=1)
    assert after["revision"] == 2
    assert all(item["orientation"] == "portrait" for item in after["package"]["cover"])

    with pytest.raises(ValueError):
        apply_ops(after, [{"op": "add", "slot": "body"}], expected_revision=1)


def test_apply_ops_drop_refills_from_pool() -> None:
    activity = {"id": "act-a", "name": "迎新晚会", "starts_at": "2026-09-20T00:00:00Z"}
    state = build_package(activity, _candidates(), slots={"cover": 3})
    first = state["package"]["cover"][0]["asset_id"]
    after = apply_ops(state, [{"op": "drop", "asset_id": first}], expected_revision=1)
    ids = [item["asset_id"] for item in after["package"]["cover"]]
    assert first not in ids
    assert len(ids) == 3


# ---- 工作流服务 ---------------------------------------------------------

class _FakeResponse:
    def __init__(self, items):
        self.results = items
        self.warnings = []
        self.degraded = False
        self.translated_query = "a photo of a stage"


def _item(asset_id: str, score: float, w: int, h: int) -> SearchResultItem:
    return SearchResultItem(
        asset_id=asset_id,
        thumbnail_url=f"http://immich.test/api/assets/{asset_id}/thumbnail",
        captured_at=None,
        time_freshness=1.0,
        final_score=score,
        metadata={"width": w, "height": h, "originalFileName": f"{asset_id}.jpg"},
    )


def _service(tmp_path: Path, ledger: Path, captured: dict) -> WorkflowService:
    async def fake_search(request: SearchRequest) -> SearchResponse:
        captured["request"] = request
        return SearchResponse(
            query=request.query,
            total=0,
            results=[
                _item("a1", 0.9, 1920, 1080),
                _item("a2", 0.8, 1080, 1920),
                _item("zzz", 0.99, 1920, 1080),  # 不在台账白名单，应被剔除
            ],
        )

    settings = Settings(
        immich_base_url="http://immich.test",
        immich_api_key="k",
        activities_path=str(ledger),
        packages_dir=str(tmp_path / "packages"),
        package_ttl_seconds=600,
    )
    store = PackageStore(settings.packages_dir, settings.package_ttl_seconds)
    return WorkflowService(settings, ActivityStore(ledger), store, search_fn=fake_search)


@pytest.mark.asyncio
async def test_search_in_activity_locks_scope(tmp_path: Path, ledger: Path) -> None:
    captured: dict = {}
    service = _service(tmp_path, ledger, captured)

    view = await service.search_in_activity(
        SearchInActivityRequest(activity_id="act-a", query="舞台 灯光", limit=50)
    )

    request = captured["request"]
    assert set(request.asset_ids) == {"a1", "a2", "a3", "a4", "a5", "a6"}
    assert "舞台" in request.query  # 活动名没有被拼进检索词

    ids = {item["asset_id"] for slot in view["package"].values() for item in slot}
    assert ids == {"a1", "a2"}  # zzz 被台账白名单剔除
    assert view["pool_size"] == 2
    assert (tmp_path / "packages" / f"{view['session_id']}.json").exists()


@pytest.mark.asyncio
async def test_edit_and_export(tmp_path: Path, ledger: Path) -> None:
    captured: dict = {}
    service = _service(tmp_path, ledger, captured)
    view = await service.search_in_activity(SearchInActivityRequest(activity_id="act-a", query="舞台"))

    edited = service.edit_package(
        PackageEditRequest(session_id=view["session_id"], revision=view["revision"], ops=[{"op": "drop", "asset_id": "a1"}])
    )
    assert edited["revision"] == view["revision"] + 1

    data, filename = await service.export_package(view["session_id"])
    assert filename.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert {"manifest.csv", "manifest.json", "README.txt"} <= names
        csv_text = zf.read("manifest.csv").decode("utf-8-sig")
        assert "asset_id" in csv_text


def test_package_store_expiry(tmp_path: Path) -> None:
    store = PackageStore(tmp_path, ttl_s=0)
    store.save({"session_id": "s1", "ttl_s": 0, "package": {}, "revision": 1})
    assert store.load("s1") is None  # 立即可过期


def test_workflow_unknown_activity_raises(tmp_path: Path, ledger: Path) -> None:
    service = _service(tmp_path, ledger, {})
    with pytest.raises(AppError):
        service.get_activity("nope")
