"""归档提案/确认与只读闸测试，外加「检索不写图库」的静态守卫。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.readonly import ensure_writable, is_readonly
from app.models.activity import ArchiveCommitRequest, ArchiveProposeRequest
from app.models.request import SearchRequest
from app.services.archive_service import ArchiveService
from app.services.package_store import PackageStore
from app.services.search_service import SearchService
from app.workflow import ActivityStore


LEDGER = {
    "version": 1,
    "activities": [
        {
            "id": "act-a",
            "name": "迎新晚会",
            "aliases": ["2026 迎新"],
            "starts_at": "2026-09-20T00:00:00Z",
            "ends_at": "2026-09-20T23:59:59Z",
            "asset_ids": ["a1", "a2", "a3"],
            "cover_asset_id": "a1",
        }
    ],
}


class _FakeImmich:
    def __init__(self, album_detail: dict | None = None) -> None:
        self.album_detail = album_detail or {"id": "alb-1", "assets": [{"id": "a1"}]}
        self.calls: list[tuple[str, str]] = []

    async def request_json(self, method: str, path: str, json=None):  # noqa: A002
        self.calls.append((method, path))
        return self.album_detail


class _Entry:
    def __init__(self, action: str, asset_id: str | None = None, success: bool = True, error: str | None = None) -> None:
        self.action = action
        self.asset_id = asset_id
        self.success = success
        self.error = error
        self.timestamp = "2026-11-21T00:00:00Z"


class _FakeWriteback:
    """最小写回替身：记录每次调用，不碰网络。"""

    def __init__(self, albums: list[dict] | None = None) -> None:
        self.albums = albums if albums is not None else [
            {"id": "alb-1", "albumName": "云影图鉴 · 迎新晚会 2026-09-20", "assetCount": 1}
        ]
        self.created: list[str] = []
        self.added: list[tuple[str, str]] = []
        self._immich = _FakeImmich()

    async def list_albums(self) -> list[dict]:
        return list(self.albums)

    async def get_or_create_album(self, name: str, dry_run: bool = False):
        self.created.append(name)
        return "alb-new", _Entry("create_album", success=True)

    async def add_to_album(self, asset_id: str, album_id: str, dry_run: bool = False):
        self.added.append((asset_id, album_id))
        return _Entry("add_to_album", asset_id, success=True)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "activities.json"
    path.write_text(json.dumps(LEDGER, ensure_ascii=False), encoding="utf-8")
    return path


def _settings(tmp_path: Path, ledger: Path, **kw) -> Settings:
    return Settings(
        immich_base_url="http://immich.test/",
        immich_api_key="k",
        activities_path=str(ledger),
        packages_dir=str(tmp_path / "packages"),
        archive_proposal_ttl_seconds=60,
        activity_album_prefix="云影图鉴 · ",
        **kw,
    )


# ---- 只读闸 -------------------------------------------------------------

def test_readonly_gate_blocks_and_marks(tmp_path: Path, ledger_path: Path) -> None:
    ro = _settings(tmp_path, ledger_path, competition_readonly=True)
    assert is_readonly(ro) is True
    with pytest.raises(AppError) as exc:
        ensure_writable(ro)
    assert exc.value.code is ErrorCode.FORBIDDEN
    assert exc.value.status_code == 403

    rw = _settings(tmp_path, ledger_path)
    ensure_writable(rw)  # 不抛


# ---- 提案 → 确认 --------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_is_read_only(tmp_path: Path, ledger_path: Path) -> None:
    settings = _settings(tmp_path, ledger_path)
    writeback = _FakeWriteback()
    service = ArchiveService(settings, writeback, ActivityStore(ledger_path))

    proposal = await service.propose(ArchiveProposeRequest(activity_id="act-a"))

    assert proposal["dry_run"] is True
    assert proposal["album"]["name"].startswith("云影图鉴 · 迎新晚会")
    assert proposal["will_add"] == 2  # 3 张里 a1 已在相册
    assert proposal["already_in"] == 1
    assert writeback.created == [] and writeback.added == []  # 提案不写
    assert proposal["expires_in"] == 60


@pytest.mark.asyncio
async def test_commit_writes_and_is_not_repeatable(tmp_path: Path, ledger_path: Path) -> None:
    settings = _settings(tmp_path, ledger_path)
    writeback = _FakeWriteback()
    service = ArchiveService(settings, writeback, ActivityStore(ledger_path))

    proposal = await service.propose(ArchiveProposeRequest(activity_id="act-a"))
    result = await service.commit(ArchiveCommitRequest(proposal_id=proposal["proposal_id"]))

    assert result["album_id"] == "alb-new"
    assert result["album_url"] == "http://immich.test/albums/alb-new"
    assert result["added"] == 3
    assert [a for a, _ in writeback.added] == ["a1", "a2", "a3"]

    with pytest.raises(AppError):
        await service.commit(ArchiveCommitRequest(proposal_id=proposal["proposal_id"]))


@pytest.mark.asyncio
async def test_propose_rejects_foreign_asset(tmp_path: Path, ledger_path: Path) -> None:
    settings = _settings(tmp_path, ledger_path)
    service = ArchiveService(settings, _FakeWriteback(), ActivityStore(ledger_path))
    with pytest.raises(AppError) as exc:
        await service.propose(ArchiveProposeRequest(activity_id="act-a", asset_ids=["a1", "outsider"]))
    assert exc.value.code is ErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_propose_from_package_session(tmp_path: Path, ledger_path: Path) -> None:
    settings = _settings(tmp_path, ledger_path)
    store = PackageStore(settings.packages_dir, 600)
    store.save({
        "session_id": "sess1",
        "revision": 1,
        "ttl_s": 600,
        "package": {"cover": [{"asset_id": "a2"}], "body": [{"asset_id": "a3"}]},
    })
    service = ArchiveService(settings, _FakeWriteback(), ActivityStore(ledger_path), store)
    proposal = await service.propose(ArchiveProposeRequest(activity_id="act-a", session_id="sess1"))
    assert proposal["will_add"] == 2


def test_unknown_proposal_raises(tmp_path: Path, ledger_path: Path) -> None:
    service = ArchiveService(_settings(tmp_path, ledger_path), _FakeWriteback(), ActivityStore(ledger_path))
    with pytest.raises(AppError):
        service.get_proposal("nope")


# ---- 检索不写图库（静态守卫）-------------------------------------------

def test_workflow_route_has_no_writeback_side_effect() -> None:
    """找图链路只读：workflow/activities 路由源码不得出现 writeback 调用。"""
    for name in ("workflow.py", "activities.py"):
        src = (Path("app/api/routes") / name).read_text(encoding="utf-8").lower()
        assert "writeback" not in src, f"{name} 不应触发写回"


def test_search_request_accepts_activity_id() -> None:
    req = SearchRequest(query="舞台", activity_id="act-a")
    assert req.activity_id == "act-a"


def test_activity_whitelist_resolution(tmp_path: Path, ledger_path: Path) -> None:
    service = object.__new__(SearchService)
    service._activities = ActivityStore(ledger_path)

    assert service._activity_asset_ids("act-a") == ["a1", "a2", "a3"]
    with pytest.raises(AppError) as exc:
        service._activity_asset_ids("missing")
    assert exc.value.code is ErrorCode.NOT_FOUND
