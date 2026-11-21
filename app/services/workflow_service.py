"""工作流编排：活动解析 → 活动内找图 → 素材包（可编辑/可导出）。

关键约束：
- 检索永远带台账白名单（activity.asset_ids），活动名不进 CLIP 查询串。
- 编辑操作只在已有候选池里重排，不重跑检索（贵且慢）。
- 每张入选图都带可验证理由（场景 / 时段 / 尺寸 / 池内位次）。
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..core.config import Settings
from ..core.errors import AppError, ErrorCode
from ..models.activity import (
    ArchiveProposeRequest,
    PackageEditRequest,
    ResolveRequest,
    SearchInActivityRequest,
)
from ..models.request import SearchRequest
from ..workflow import ActivityStore
from ..workflow.package import (
    DEFAULT_SLOTS,
    apply_ops,
    build_package,
    manifest_csv,
    manifest_json,
    public_view,
    summarize_diff,
)
from .package_store import PackageStore

logger = logging.getLogger(__name__)

ThumbFetcher = Callable[[str], Awaitable[bytes]]


def _num(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidate(item: Any) -> dict[str, Any]:
    """把 SearchResultItem 映射成素材包候选（宽松解析 metadata）。"""
    meta: dict[str, Any] = getattr(item, "metadata", None) or {}
    exif = meta.get("exifInfo") if isinstance(meta.get("exifInfo"), dict) else {}
    w = _num(meta.get("width") or exif.get("exifImageWidth") or exif.get("width"))
    h = _num(meta.get("height") or exif.get("exifImageHeight") or exif.get("height"))
    scene_obj = getattr(item, "scene", None)
    scene = None
    scene_sim = None
    if scene_obj is not None:
        scene = getattr(scene_obj, "primary", None) or getattr(scene_obj, "primary_scene", None)
        scene_sim = getattr(scene_obj, "confidence", None) or getattr(scene_obj, "similarity", None)
    score = getattr(item, "final_score", None)
    if score is None:
        score = getattr(item, "similarity_score", None)
    return {
        "asset_id": str(getattr(item, "asset_id", "")),
        "score": round(float(score), 4) if isinstance(score, (int, float)) else None,
        "w": w,
        "h": h,
        "thumb": getattr(item, "thumbnail_url", None),
        "taken_at": getattr(item, "captured_at", None),
        "scene": scene,
        "scene_sim": round(float(scene_sim), 3) if isinstance(scene_sim, (int, float)) else None,
        "fileName": str(meta.get("originalFileName") or meta.get("fileName") or ""),
    }


class WorkflowService:
    """活动内找图 → 素材包 → 编辑 → 导出。"""

    def __init__(
        self,
        settings: Settings,
        activity_store: ActivityStore,
        package_store: PackageStore,
        search_service: Any | None = None,
        search_fn: Callable[[SearchRequest], Awaitable[Any]] | None = None,
        thumb_fetcher: ThumbFetcher | None = None,
    ) -> None:
        self._settings = settings
        self._activities = activity_store
        self._packages = package_store
        self._search_service = search_service
        self._search_fn = search_fn or self._default_search
        self._thumb_fetcher = thumb_fetcher

    async def _default_search(self, request: SearchRequest) -> Any:
        if self._search_service is None:
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "搜索服务未初始化")
        return await self._search_service.search(request)

    # ---- 台账 ---------------------------------------------------------
    def list_activities(self) -> dict[str, Any]:
        acts = self._activities.list()
        warnings: list[str] = []
        if not acts:
            warnings.append(f"活动台账为空或未找到：{self._activities.path}")
        return {
            "total": len(acts),
            "activities": [a.to_summary() for a in acts],
            "source": str(self._activities.path),
            "warnings": warnings,
        }

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        act = self._activities.get(activity_id)
        if act is None:
            raise AppError(ErrorCode.NOT_FOUND, f"活动不存在: {activity_id}")
        return act.to_dict()

    def resolve(self, payload: ResolveRequest) -> dict[str, Any]:
        outcome = self._activities.resolve(
            query=payload.utterance or payload.query,
            date=payload.date,
            name=payload.name,
        )
        return outcome.to_dict()

    # ---- 找图 + 建包 ---------------------------------------------------
    async def search_in_activity(self, payload: SearchInActivityRequest) -> dict[str, Any]:
        act = self._activities.get(payload.activity_id)
        if act is None:
            raise AppError(ErrorCode.NOT_FOUND, f"活动不存在: {payload.activity_id}")
        if not act.asset_ids:
            raise AppError(ErrorCode.INVALID_PARAMS, f"活动 {act.id} 没有登记任何照片")

        query = (payload.query or "").strip() or act.name
        request = SearchRequest(
            query=query,
            asset_ids=list(act.asset_ids),
            limit=max(1, min(int(payload.limit), 200)),
        )
        response = await self._search_fn(request)
        results = list(getattr(response, "results", []) or [])
        candidates = [_candidate(item) for item in results]
        candidates = [c for c in candidates if c["asset_id"]]

        allowed = set(act.asset_ids)
        candidates = [c for c in candidates if c["asset_id"] in allowed]

        state = build_package(
            activity=act.to_dict(),
            candidates=candidates,
            slots=payload.slots or DEFAULT_SLOTS,
            orientation=payload.orientation,
            ttl_s=int(self._settings.package_ttl_seconds),
        )
        self._packages.save(state)

        view = public_view(state)
        view["query"] = query
        view["pool_size"] = len(candidates)
        view["warnings"] = list(getattr(response, "warnings", []) or [])
        view["degraded"] = bool(getattr(response, "degraded", False))
        view["translated_query"] = getattr(response, "translated_query", None)
        if not candidates:
            view.setdefault("warnings", []).append("活动范围内没有命中任何照片，可换描述词或放宽 limit")
        return view

    # ---- 会话存取 ------------------------------------------------------
    def get_session(self, session_id: str) -> dict[str, Any]:
        state = self._packages.load(session_id)
        if state is None:
            raise AppError(ErrorCode.NOT_FOUND, f"素材包会话不存在或已过期: {session_id}")
        return state

    def view_session(self, session_id: str) -> dict[str, Any]:
        return public_view(self.get_session(session_id))

    def edit_package(self, payload: PackageEditRequest) -> dict[str, Any]:
        state = self.get_session(payload.session_id)
        before = {"package": {k: list(v) for k, v in (state.get("package") or {}).items()}}
        try:
            new_state = apply_ops(state, payload.ops, expected_revision=payload.revision)
        except ValueError as exc:
            raise AppError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        self._packages.save(new_state)
        view = public_view(new_state)
        view["changes"] = new_state.get("changes", [])
        view["diff"] = summarize_diff(before, new_state)
        return view

    # ---- 导出 ---------------------------------------------------------
    async def export_package(self, session_id: str) -> tuple[bytes, str]:
        """返回 (zip 字节, 文件名)。含 manifest.csv / manifest.json / 缩略图。"""
        state = self.get_session(session_id)
        rows = manifest_json(state).get("rows") or []
        buf = io.BytesIO()
        missing: list[str] = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.csv", manifest_csv(state))
            zf.writestr("manifest.json", __import__("json").dumps(manifest_json(state), ensure_ascii=False, indent=2))
            if self._thumb_fetcher is not None:
                for idx, row in enumerate(rows, start=1):
                    url = row.get("thumb")
                    if not url:
                        continue
                    try:
                        data = await self._thumb_fetcher(str(url))
                    except Exception as exc:  # 单张失败不阻塞整包
                        logger.warning("缩略图抓取失败 %s: %s", url, exc)
                        missing.append(str(row.get("asset_id")))
                        continue
                    slot = row.get("slot") or "item"
                    name = f"thumbs/{slot}-{idx:02d}-{row.get('asset_id')}.jpg"
                    zf.writestr(name, data)
            if missing:
                zf.writestr("MISSING.txt", "以下资产缩略图抓取失败：\n" + "\n".join(missing))
            zf.writestr(
                "README.txt",
                "云影图鉴素材包\n"
                f"活动：{(state.get('activity') or {}).get('name', '')}\n"
                f"会话：{state.get('session_id')} 修订：{state.get('revision')}\n"
                "manifest.csv 可用 Excel 直接打开（含选图理由）。\n",
            )
        activity = (state.get("activity") or {}).get("name") or "package"
        slug = "".join(ch for ch in activity if ch.isalnum() or ch in "-_")[:20] or "package"
        filename = f"{slug}-{state.get('session_id')}-r{state.get('revision')}.zip"
        return buf.getvalue(), filename

    # ---- 归档入口（供 archive service 复用素材包）------------------------
    def collect_asset_ids(self, payload: ArchiveProposeRequest) -> list[str]:
        if payload.asset_ids:
            return [str(a) for a in payload.asset_ids]
        if payload.session_id:
            state = self.get_session(payload.session_id)
            ids: list[str] = []
            for items in (state.get("package") or {}).values():
                for item in items:
                    aid = str(item.get("asset_id"))
                    if aid and aid not in ids:
                        ids.append(aid)
            return ids
        return []
