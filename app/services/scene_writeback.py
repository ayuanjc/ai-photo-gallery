"""场景标签写回服务：将搜索结果中的 AI 场景标签写回 Immich。

流程：
1. 从搜索结果提取每张图片的场景标签（primary_scene + scenes + sub_scenes）
2. 白名单过滤：只写 source=analyzed 且置信度 >= scene_min_confidence 的标签
   （查询推断/兜底 fallback 的猜测场景只用于前端展示，写回会把猜测当事实污染图库）
3. 为每个场景生成 AI/<场景中文名> 标签（子场景用 _SUB_SCENE_LABELS 映射）
4. 通过 ImmichWriteBack 幂等写入 Immich（Redis SETNX 窗口 + Immich find-or-create）
5. 非阻塞：失败仅记日志，不影响搜索结果

触发方式：搜索路由返回结果后，作为 BackgroundTask 执行。
"""
from __future__ import annotations

import logging
from typing import Any

from ..core.config import Settings
from ..scene.dictionary import get_scene_dict
from .immich_writeback import ImmichWriteBack, WriteBackEntry

logger = logging.getLogger(__name__)

_AI_PREFIX = "AI·"

# 子场景 key → 中文标签（词典 children 无中文 label，写回标签用此映射）。
_SUB_SCENE_LABELS: dict[str, str] = {
    # nature
    "beach": "海滩", "mountain": "山脉", "forest": "森林", "lake": "湖泊",
    "desert": "沙漠", "snow": "雪景", "ocean": "海洋", "sky": "天空", "flower": "花卉",
    # city
    "street": "街道", "building": "高楼", "night_view": "城市夜色", "traffic": "车流",
    "landmark": "地标",
    # people
    "single": "单人", "group": "合影", "wedding": "婚礼", "party": "派对",
    "child": "儿童", "portrait": "人像", "family": "家庭",
    # life
    "food": "美食", "pet": "宠物", "home": "家居", "office": "办公", "sport": "运动",
    # event
    "festival": "节日", "celebration": "庆祝", "performance": "演出",
    "travel": "旅行", "graduation": "毕业",
    # animal
    "dog": "狗", "cat": "猫", "bird": "鸟类", "wildlife": "野生动物",
    "marine": "海洋生物", "insect": "昆虫",
    # architecture
    "temple": "庙宇", "bridge": "桥梁", "modern": "现代建筑",
    "historic": "历史建筑", "interior": "室内",
    # night
    "starry": "星空", "city_lights": "城市灯光", "moon": "月亮",
    "fireworks": "烟花", "neon": "霓虹",
    # vehicle
    "car": "汽车", "bike": "自行车", "boat": "船只", "plane": "飞机", "train": "火车",
    # art
    "painting": "画作", "sculpture": "雕塑", "graffiti": "涂鸦",
    "design": "设计", "craft": "手工",
}


class SceneWriteBack:
    def __init__(self, writeback: ImmichWriteBack, settings: Settings) -> None:
        self._writeback = writeback
        self._settings = settings
        # 白名单置信度下限：与场景推理共用同一旋钮（默认 0.60）。
        self._min_conf = float(getattr(settings, "scene_min_confidence", 0.60))

    async def writeback_scene_tags(
        self,
        results: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> list[WriteBackEntry]:
        if not self._settings.writeback_enabled:
            return []

        scene_dict = self._load_scene_dict()
        entries: list[WriteBackEntry] = []
        skipped_fallback = 0
        skipped_low_conf = 0

        for result in results:
            asset_id = result.get("asset_id")
            if not asset_id:
                continue

            scene_tags = result.get("scene")

            # 白名单第一关：非 analyzed（查询推断/兜底）一律不写。
            if self._extract_source(scene_tags) != "analyzed":
                if scene_tags is not None:
                    skipped_fallback += 1
                continue

            # 白名单第二关：置信度低于下限的标签不写。
            confidence = self._extract_confidence(scene_tags)

            def _conf_ok(key: str, confidence: dict = confidence) -> bool:
                conf = confidence.get(key)
                return conf is not None and float(conf) >= self._min_conf

            all_scenes = self._extract_scenes(scene_tags)
            all_subs = self._extract_sub_scenes(scene_tags)
            scenes = [s for s in all_scenes if _conf_ok(s)]
            subs = [s for s in all_subs if _conf_ok(s)]
            skipped_low_conf += (len(all_scenes) - len(scenes)) + (
                len(all_subs) - len(subs)
            )
            if not scenes and not subs:
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

            for sub_key in subs:
                tag_name = f"{_AI_PREFIX}{_SUB_SCENE_LABELS.get(sub_key, sub_key)}"
                try:
                    entry = await self._writeback.add_tag(
                        asset_id, tag_name, dry_run=dry_run
                    )
                    entries.append(entry)
                except Exception as exc:
                    logger.warning(
                        "子场景标签写回异常 asset=%s sub=%s: %s",
                        asset_id, sub_key, exc,
                    )

        succeeded = sum(1 for e in entries if e.success)
        logger.info(
            "场景标签写回完成: %d 条操作, %d 成功, %d 失败, "
            "白名单拦截(非analyzed)=%d, 白名单拦截(低置信)=%d",
            len(entries), succeeded, len(entries) - succeeded,
            skipped_fallback, skipped_low_conf,
        )
        return entries

    @staticmethod
    def _extract_source(scene_tags: Any) -> str | None:
        if scene_tags is None:
            return None
        if isinstance(scene_tags, dict):
            return scene_tags.get("source")
        return getattr(scene_tags, "source", None)

    @staticmethod
    def _extract_confidence(scene_tags: Any) -> dict[str, Any]:
        if scene_tags is None:
            return {}
        if isinstance(scene_tags, dict):
            conf = scene_tags.get("confidence")
            return conf if isinstance(conf, dict) else {}
        conf = getattr(scene_tags, "confidence", None)
        return conf if isinstance(conf, dict) else {}

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

    @staticmethod
    def _extract_sub_scenes(scene_tags: Any) -> list[str]:
        if scene_tags is None:
            return []
        if isinstance(scene_tags, dict):
            subs = list(scene_tags.get("sub_scenes") or [])
        else:
            subs = list(getattr(scene_tags, "sub_scenes", None) or [])
        return [s for s in subs if s]

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
