"""场景标签写回服务：将搜索结果中的 AI 场景标签写回 Immich。

流程：
1. 从搜索结果提取每张图片的场景标签（primary_scene + scenes）
2. 为每个场景生成 AI/<场景中文名> 标签
3. 通过 ImmichWriteBack 幂等写入 Immich
4. 非阻塞：失败仅记日志，不影响搜索结果

触发方式：搜索路由返回结果后，作为 BackgroundTask 执行。
"""
from __future__ import annotations

import logging
from typing import Any

from ..core.config import Settings
from ..core.runtime_config import runtime_enabled
from ..scene.dictionary import get_scene_dict
from .immich_writeback import ImmichWriteBack, WriteBackEntry

logger = logging.getLogger(__name__)

_AI_PREFIX = "AI·"


class SceneWriteBack:
    def __init__(self, writeback: ImmichWriteBack, settings: Settings) -> None:
        self._writeback = writeback
        self._settings = settings

    async def writeback_scene_tags(
        self,
        results: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> list[WriteBackEntry]:
        if not runtime_enabled(
            "writeback_enabled", getattr(self._settings, "writeback_enabled", False)
        ):
            return []

        scene_dict = self._load_scene_dict()
        entries: list[WriteBackEntry] = []

        for result in results:
            asset_id = result.get("asset_id")
            if not asset_id:
                continue

            scene_tags = result.get("scene")
            scenes = self._extract_scenes(scene_tags)
            if not scenes:
                continue

            for scene_key in scenes:
                label = self._scene_label(scene_key, scene_dict)
                tag_name = f"{_AI_PREFIX}{label}"
                try:
                    entry = await self._writeback.add_tag(
                        asset_id, tag_name, dry_run=dry_run
                    )
                    entries.append(entry)
                except Exception as exc:
                    logger.warning(
                        "场景标签写回异常 asset=%s scene=%s: %s",
                        asset_id, scene_key, exc,
                    )

        succeeded = sum(1 for e in entries if e.success)
        logger.info(
            "场景标签写回完成: %d 条操作, %d 成功, %d 失败",
            len(entries), succeeded, len(entries) - succeeded,
        )
        return entries

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
