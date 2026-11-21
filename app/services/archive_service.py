"""归档编排：先提案（dry-run），用户确认后再写图库。

两步的意义：写回是外部副作用，不可撤销（除 admin 回滚），所以
- propose 只读：算相册名、查重名相册、统计将新增多少，返回 proposal_id（TTL 内有效）；
- commit 才真正 get_or_create_album + 逐资产 add_to_album，并返回相册链接。
只读模式（COMPETITION_READONLY=true）下 propose/commit 全被拦（见 app/core/readonly.py）。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from ..core.config import Settings
from ..core.errors import AppError, ErrorCode
from ..models.activity import ArchiveCommitRequest, ArchiveProposeRequest
from ..workflow import ActivityStore
from .package_store import PackageStore

logger = logging.getLogger(__name__)


class ArchiveService:
    """活动照片归档到 Immich 相册（提案 → 确认）。"""

    def __init__(
        self,
        settings: Settings,
        writeback: Any,
        activity_store: ActivityStore,
        package_store: PackageStore | None = None,
    ) -> None:
        self._settings = settings
        self._writeback = writeback
        self._activities = activity_store
        self._packages = package_store
        self._proposals: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ---- 工具 ---------------------------------------------------------
    def _album_url(self, album_id: str | None) -> str | None:
        if not album_id:
            return None
        base = str(getattr(self._settings, "immich_base_url", "") or "").rstrip("/")
        return f"{base}/albums/{album_id}" if base else None

    def _purge(self) -> None:
        now = time.time()
        with self._lock:
            for pid in [k for k, v in self._proposals.items() if v["expires_at"] < now]:
                self._proposals.pop(pid, None)

    def _collect_ids(self, payload: ArchiveProposeRequest, act: Any) -> list[str]:
        """确定本次要归档哪些照片：显式 asset_ids > 素材包会话 > 该活动台账全部照片。"""
        if payload.asset_ids:
            return [str(a) for a in payload.asset_ids]
        if payload.session_id and self._packages is not None:
            state = self._packages.load(payload.session_id)
            if state is None:
                raise AppError(ErrorCode.NOT_FOUND, f"素材包会话不存在或已过期: {payload.session_id}")
            ids: list[str] = []
            for items in (state.get("package") or {}).values():
                for item in items:
                    aid = str(item.get("asset_id"))
                    if aid and aid not in ids:
                        ids.append(aid)
            return ids
        # 都没给：默认归档该活动台账登记的全部照片（整场归档是最常见诉求）
        return list(act.asset_ids)

    # ---- 提案 ---------------------------------------------------------
    async def propose(self, payload: ArchiveProposeRequest) -> dict[str, Any]:
        act = self._activities.get(payload.activity_id)
        if act is None:
            raise AppError(ErrorCode.NOT_FOUND, f"活动不存在: {payload.activity_id}")

        asset_ids = self._collect_ids(payload, act)
        if not asset_ids:
            raise AppError(ErrorCode.INVALID_PARAMS, "没有可归档的照片：请给 asset_ids 或 session_id")

        allowed = set(act.asset_ids)
        outside = [a for a in asset_ids if allowed and a not in allowed]
        if outside:
            raise AppError(
                ErrorCode.INVALID_PARAMS,
                f"{len(outside)} 张照片不属于活动「{act.name}」，归档前请确认范围",
                details={"outside": outside[:20]},
            )

        album_name = act.album_name().replace("云影图鉴 · ", str(self._settings.activity_album_prefix))
        album_id: str | None = None
        already_in = 0
        existing_asset_count = 0
        warnings: list[str] = []
        try:
            albums = await self._writeback.list_albums()
        except Exception as exc:
            albums = []
            warnings.append(f"读取相册列表失败（将按新建处理）: {exc}")
        for album in albums:
            if str(album.get("albumName") or "") == album_name:
                album_id = str(album.get("id") or "") or None
                existing_asset_count = int(album.get("assetCount") or 0)
                break

        if album_id:
            warnings.append(f"相册已存在（{existing_asset_count} 张），确认后只补缺失照片")
            try:
                detail = await self._writeback._immich.request_json("GET", f"/api/albums/{album_id}")
                if isinstance(detail, dict) and isinstance(detail.get("assets"), list):
                    present = {str(a.get("id")) for a in detail["assets"] if isinstance(a, dict)}
                    already_in = len([a for a in asset_ids if a in present])
            except Exception as exc:
                warnings.append(f"无法确认相册内现有照片，按全部补入处理: {exc}")

        will_add = max(0, len(asset_ids) - already_in)
        ttl = int(self._settings.archive_proposal_ttl_seconds)
        proposal_id = uuid.uuid4().hex[:16]
        self._purge()
        with self._lock:
            self._proposals[proposal_id] = {
                "activity_id": act.id,
                "activity_name": act.name,
                "album_name": album_name,
                "album_id": album_id,
                "asset_ids": asset_ids,
                "created_at": time.time(),
                "expires_at": time.time() + ttl,
                "committed": False,
            }

        return {
            "proposal_id": proposal_id,
            "dry_run": True,
            "album": {
                "id": album_id,
                "name": album_name,
                "exists": album_id is not None,
                "asset_count": existing_asset_count,
                "url": self._album_url(album_id),
            },
            "will_add": will_add,
            "already_in": already_in,
            "tags": [act.name],
            "warnings": warnings,
            "expires_in": ttl,
        }

    # ---- 确认写入 ------------------------------------------------------
    async def commit(self, payload: ArchiveCommitRequest) -> dict[str, Any]:
        with self._lock:
            proposal = self._proposals.get(payload.proposal_id)
        if proposal is None:
            raise AppError(ErrorCode.NOT_FOUND, "归档提案不存在或已失效，请重新 propose")
        if proposal["expires_at"] < time.time():
            with self._lock:
                self._proposals.pop(payload.proposal_id, None)
            raise AppError(ErrorCode.INVALID_PARAMS, "归档提案已过期，请重新 propose")
        if proposal.get("committed"):
            raise AppError(ErrorCode.INVALID_PARAMS, "该提案已执行过，请重新 propose 再确认")

        album_id, entry = await self._writeback.get_or_create_album(proposal["album_name"])
        if not album_id:
            raise AppError(
                ErrorCode.UPSTREAM_ERROR,
                f"相册创建失败: {entry.error or '未知原因'}",
            )

        added = 0
        skipped = 0
        failed: list[dict[str, Any]] = []
        audit_ids: list[str] = []
        for asset_id in proposal["asset_ids"]:
            result = await self._writeback.add_to_album(asset_id, album_id)
            if result.success:
                added += 1
                if result.error:
                    skipped += 1
            else:
                failed.append({"asset_id": asset_id, "error": result.error or "写回失败"})
            audit_ids.append(f"{result.action}:{result.asset_id}:{result.timestamp}")

        with self._lock:
            proposal["committed"] = True
            proposal["committed_at"] = time.time()

        return {
            "album_id": album_id,
            "album_url": self._album_url(album_id),
            "album_name": proposal["album_name"],
            "added": added,
            "skipped": skipped,
            "failed": failed,
            "audit_ids": audit_ids,
        }

    # ---- 提案查询（便于前端在确认前展示）---------------------------------
    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise AppError(ErrorCode.NOT_FOUND, "归档提案不存在或已失效")
        data = {k: v for k, v in proposal.items() if k != "asset_ids"}
        data["asset_count"] = len(proposal["asset_ids"])
        data["remaining_s"] = max(0, int(proposal["expires_at"] - time.time()))
        return data
