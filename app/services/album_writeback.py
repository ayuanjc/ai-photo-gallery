"""AI 相册写回服务：按场景+月份自动创建相册并添加资产。

相册命名规则：AI·<场景中文名>·<YYYY-MM>
例如：AI·自然·2026-10

流程：
1. 从搜索结果提取每张图片的场景标签和拍摄时间
2. 按 (场景, 月份) 分组
3. 对每组：get_or_create_album → add_to_album（幂等）
4. 非阻塞：失败仅记日志
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..core.config import Settings
from ..core.runtime_config import runtime_enabled
from ..scene.dictionary import get_scene_dict
from .immich_writeback import ImmichWriteBack, WriteBackEntry

logger = logging.getLogger(__name__)

_ALBUM_PREFIX = "AI·"


class AlbumWriteBack:
    def __init__(self, writeback: ImmichWriteBack, settings: Settings) -> None:
        self._writeback = writeback
        self._settings = settings

    async def writeback_albums(
        self,
        results: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> list[WriteBackEntry]:
        if not runtime_enabled(
            "writeback_enabled", getattr(self._settings, "writeback_enabled", False)
        ):
            return []

        scene_dict = self._load_scene_dict()
        groups = self._group_by_scene_month(results, scene_dict)
        if not groups:
            return []

        entries: list[WriteBackEntry] = []
        album_cache: dict[str, str | None] = {}

        for (scene_label, month_str), asset_ids in groups.items():
            album_name = f"{_ALBUM_PREFIX}{scene_label}·{month_str}"
            if album_name not in album_cache:
                try:
                    album_id, create_entry = await self._writeback.get_or_create_album(
                        album_name, dry_run=dry_run
                    )
                    album_cache[album_name] = album_id
                    entries.append(create_entry)
                except Exception as exc:
                    logger.warning("相册创建失败 name=%s: %s", album_name, exc)
                    album_cache[album_name] = None
                    continue

            album_id = album_cache[album_name]
            if not album_id or dry_run:
                continue

            for asset_id in asset_ids:
                try:
                    entry = await self._writeback.add_to_album(
                        asset_id, album_id, dry_run=dry_run
                    )
                    entries.append(entry)
                except Exception as exc:
                    logger.warning(
                        "相册添加失败 asset=%s album=%s: %s",
                        asset_id, album_name, exc,
                    )

        succeeded = sum(1 for e in entries if e.success)
        logger.info(
            "AI相册写回完成: %d 条操作, %d 成功, %d 失败",
            len(entries), succeeded, len(entries) - succeeded,
        )
        return entries

    def _group_by_scene_month(
        self,
        results: list[dict[str, Any]],
        scene_dict: Any,
    ) -> dict[tuple[str, str], list[str]]:
        groups: dict[tuple[str, str], list[str]] = {}
        for result in results:
            asset_id = result.get("asset_id")
            if not asset_id:
                continue

            scene_tags = result.get("scene")
            scenes = self._extract_scenes(scene_tags)
            if not scenes:
                continue

            month_str = self._extract_month(result)

            for scene_key in scenes:
                label = self._scene_label(scene_key, scene_dict)
                key = (label, month_str)
                groups.setdefault(key, []).append(asset_id)

        return groups

    def _extract_scenes(self, scene_tags: Any) -> list[str]:
        if scene_tags is None:
            return []
        if isinstance(scene_tags, dict):
            scenes = list(scene_tags.get("scenes") or [])
            primary = scene_tags.get("primary_scene")
            if primary and primary not in scenes and primary != "default":
                scenes.insert(0, primary)
            return [s for s in scenes if s and s != "default"]
        scenes = list(getattr(scene_tags, "scenes", None) or [])
        primary = getattr(scene_tags, "primary_scene", None)
        if primary and primary not in scenes and primary != "default":
            scenes.insert(0, primary)
        return [s for s in scenes if s and s != "default"]

    def _extract_month(self, result: dict[str, Any]) -> str:
        captured = result.get("captured_at")
        if captured:
            try:
                if isinstance(captured, str):
                    dt = datetime.fromisoformat(captured.replace("Z", "+00:00"))
                elif isinstance(captured, datetime):
                    dt = captured
                else:
                    dt = None
                if dt:
                    return dt.strftime("%Y-%m")
            except (ValueError, TypeError):
                pass
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _load_scene_dict(self) -> Any:
        try:
            return get_scene_dict()
        except Exception:
            return None

    def _scene_label(self, scene_key: str, scene_dict: Any) -> str:
        if scene_dict is None:
            return scene_key
        try:
            entry = scene_dict.scenes.get(scene_key)
            if entry and entry.get("label"):
                return str(entry["label"])
        except Exception:
            pass
        return scene_key

