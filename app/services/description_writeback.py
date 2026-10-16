"""描述/评分写回服务：将 AI 生成的描述与评分写回 Immich。

流程：
1. 从搜索结果提取每张图片的场景标签与排序分数
2. 生成 AI 描述（场景 + 视觉属性 + 时间）
3. 将 final_score 映射为 1-5 星评分
4. 通过 ImmichWriteBack 幂等写入 Immich
5. 非阻塞：失败仅记日志，不影响搜索结果

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

_SCENE_LABELS = {
    "nature": "自然", "city": "城市", "people": "人物", "life": "生活",
    "event": "事件", "animal": "动物", "architecture": "建筑", "night": "夜景",
    "vehicle": "车辆", "art": "艺术",
}
_SUB_SCENE_LABELS = {
    "beach": "海滩", "mountain": "山景", "forest": "森林", "lake": "湖泊",
    "desert": "沙漠", "snow": "雪景", "ocean": "海洋", "sky": "天空", "flower": "花卉",
    "street": "街道", "building": "建筑", "night_view": "夜景", "traffic": "交通", "landmark": "地标",
    "single": "单人", "group": "群像", "wedding": "婚礼", "party": "派对", "child": "儿童",
    "portrait": "肖像", "family": "家庭",
    "food": "美食", "pet": "宠物", "home": "家居", "office": "办公", "sport": "运动",
    "festival": "节日", "celebration": "庆典", "performance": "演出", "travel": "旅行", "graduation": "毕业",
    "dog": "狗", "cat": "猫", "bird": "鸟", "wildlife": "野生动物", "marine": "海洋生物", "insect": "昆虫",
    "temple": "寺庙", "bridge": "桥梁", "modern": "现代建筑", "historic": "历史建筑", "interior": "室内",
    "starry": "星空", "city_lights": "城市灯火", "moon": "月亮", "fireworks": "烟花", "neon": "霓虹",
    "car": "汽车", "bike": "自行车", "boat": "船", "plane": "飞机", "train": "火车",
    "painting": "画作", "sculpture": "雕塑", "graffiti": "涂鸦", "design": "设计", "craft": "手工艺",
}
_TIME_LABELS = {"day": "白天", "night": "夜晚", "sunrise": "日出", "sunset": "日落"}
_PEOPLE_LABELS = {"none": "无人", "single": "一人", "group": "多人"}
_INDOOR_LABELS = {"indoor": "室内", "outdoor": "室外"}
_WEATHER_LABELS = {"sunny": "晴天", "cloudy": "多云", "rainy": "雨天", "snow": "雪天"}


class DescriptionWriteBack:
    def __init__(self, writeback: ImmichWriteBack, settings: Settings) -> None:
        self._writeback = writeback
        self._settings = settings

    async def writeback_descriptions(
        self,
        results: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> list[WriteBackEntry]:
        if not runtime_enabled(
            "writeback_enabled", getattr(self._settings, "writeback_enabled", False)
        ):
            return []

        entries: list[WriteBackEntry] = []

        for result in results:
            asset_id = result.get("asset_id")
            if not asset_id:
                continue

            description = self._generate_description(result)
            rating = self._calculate_rating(result)

            if description:
                try:
                    entry = await self._writeback.update_description(
                        asset_id, description, dry_run=dry_run
                    )
                    entries.append(entry)
                except Exception as exc:
                    logger.warning("描述写回异常 asset=%s: %s", asset_id, exc)

            if rating > 0:
                try:
                    entry = await self._writeback.update_rating(
                        asset_id, rating, dry_run=dry_run
                    )
                    entries.append(entry)
                except Exception as exc:
                    logger.warning("评分写回异常 asset=%s: %s", asset_id, exc)

        succeeded = sum(1 for e in entries if e.success)
        logger.info(
            "描述/评分写回完成: %d 条操作, %d 成功, %d 失败",
            len(entries), succeeded, len(entries) - succeeded,
        )
        return entries

    def _generate_description(self, result: dict[str, Any]) -> str:
        scene_tags = result.get("scene")
        if not scene_tags:
            return ""

        parts: list[str] = []

        if isinstance(scene_tags, dict):
            primary = scene_tags.get("primary_scene")
            scenes = scene_tags.get("scenes") or []
            sub_scenes = scene_tags.get("sub_scenes") or []
            visual_attrs = scene_tags.get("visual_attrs") or {}
        else:
            primary = getattr(scene_tags, "primary_scene", None)
            scenes = getattr(scene_tags, "scenes", None) or []
            sub_scenes = getattr(scene_tags, "sub_scenes", None) or []
            visual_attrs = getattr(scene_tags, "visual_attrs", None) or {}

        scene_labels: list[str] = []
        all_scenes = list(scenes)
        if primary and primary not in all_scenes and primary != "default":
            all_scenes.insert(0, primary)
        for s in all_scenes:
            if s and s != "default":
                label = _SCENE_LABELS.get(s, s)
                if label not in scene_labels:
                    scene_labels.append(label)
        if scene_labels:
            parts.append(" · ".join(scene_labels))

        sub_labels: list[str] = []
        for ss in sub_scenes:
            label = _SUB_SCENE_LABELS.get(ss, ss)
            if label not in sub_labels:
                sub_labels.append(label)
        if sub_labels:
            parts.append(" · ".join(sub_labels))

        attr_labels: list[str] = []
        if isinstance(visual_attrs, dict):
            for key, val in visual_attrs.items():
                label = self._attr_label(key, val)
                if label and label not in attr_labels:
                    attr_labels.append(label)
        if attr_labels:
            parts.append(" · ".join(attr_labels))

        if not parts:
            return ""

        return f"AI 分析：{' / '.join(parts)}"

    def _calculate_rating(self, result: dict[str, Any]) -> int:
        score = result.get("final_score")
        if score is None:
            score = result.get("similarity_score")
        if score is None:
            return 0
        try:
            score = float(score)
        except (TypeError, ValueError):
            return 0
        rating = int(score * 5 + 0.5)
        return max(1, min(5, rating))

    def _attr_label(self, key: str, val: str) -> str:
        if not val:
            return ""
        if key == "time":
            return _TIME_LABELS.get(val, "")
        if key == "people":
            return _PEOPLE_LABELS.get(val, "")
        if key == "indoor":
            return _INDOOR_LABELS.get(val, "")
        if key == "weather":
            return _WEATHER_LABELS.get(val, "")
        return ""