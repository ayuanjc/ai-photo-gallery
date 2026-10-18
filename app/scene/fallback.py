"""场景兜底：分析与推理均无结果时，保证每条结果仍有可用的场景标签。

兜底顺序：
1. 查询推断 —— 用查询词匹配场景提示词表/词典中文 label（搜索场景下结果
   本就按 query 检索而来，以查询推断场景比盲目兜底更贴合结果集语义）。
2. 默认场景 —— settings.plugin_fallback_scene（须在词典白名单内，空 = 禁用）。

已有场景内容时原样返回，不覆盖真实分析结果。
"""
from __future__ import annotations

import logging
from typing import Any

from ..core.config import Settings
from .dictionary import SceneDict, get_scene_dict
from .models import SceneTags

logger = logging.getLogger(__name__)

# 查询推断的置信度（低于真实分析 1.0 / 推理 0.6，高于盲兜底 0.1）。
_QUERY_CONFIDENCE = 0.4
# 默认兜底场景的置信度。
_FALLBACK_CONFIDENCE = 0.1

# 查询提示词表：(关键词, 大场景, 小场景或 None)。
# 顺序即优先级；小场景会自动补齐其父大场景。仅收录中文高频摄影词汇。
_QUERY_SCENE_HINTS: list[tuple[str, str, str | None]] = [
    # ---- 夜景（先于城市，"夜景"优先归 night 而非 city.night_view）----
    ("夜景", "night", None), ("夜晚", "night", None), ("夜色", "night", None),
    ("晚上", "night", None), ("深夜", "night", None),
    ("星空", "night", "starry"), ("银河", "night", "starry"), ("星星", "night", "starry"),
    ("月亮", "night", "moon"), ("明月", "night", "moon"),
    ("烟花", "night", "fireworks"), ("焰火", "night", "fireworks"),
    ("霓虹", "night", "neon"), ("灯光", "night", "city_lights"),
    # ---- 自然 ----
    ("海边", "nature", "beach"), ("沙滩", "nature", "beach"), ("海滩", "nature", "beach"),
    ("日出", "nature", "sky"), ("日落", "nature", "sky"), ("夕阳", "nature", "sky"),
    ("晚霞", "nature", "sky"), ("朝霞", "nature", "sky"), ("天空", "nature", "sky"),
    ("山", "nature", "mountain"), ("雪山", "nature", "mountain"),
    ("森林", "nature", "forest"), ("树林", "nature", "forest"),
    ("湖", "nature", "lake"), ("湖泊", "nature", "lake"),
    ("海", "nature", "ocean"), ("大海", "nature", "ocean"),
    ("雪", "nature", "snow"), ("雪景", "nature", "snow"),
    ("沙漠", "nature", "desert"),
    ("花", "nature", "flower"), ("花卉", "nature", "flower"),
    ("草原", "nature", None), ("瀑布", "nature", None),
    # ---- 城市 ----
    ("城市", "city", None), ("都市", "city", None), ("街头", "city", "street"),
    ("街道", "city", "street"), ("马路", "city", "street"),
    ("大楼", "city", "building"), ("高楼", "city", "building"),
    ("车流", "city", "traffic"), ("地标", "city", "landmark"),
    # ---- 人物 ----
    ("人物", "people", None), ("人像", "people", "portrait"), ("肖像", "people", "portrait"),
    ("自拍", "people", "single"), ("合影", "people", "group"), ("合照", "people", "group"),
    ("全家福", "people", "family"), ("家人", "people", "family"),
    ("婚礼", "people", "wedding"), ("婚纱", "people", "wedding"),
    ("派对", "people", "party"), ("聚会", "people", "party"),
    ("孩子", "people", "child"), ("儿童", "people", "child"), ("小孩", "people", "child"),
    # ---- 生活 ----
    ("生活", "life", None), ("美食", "life", "food"), ("食物", "life", "food"),
    ("咖啡", "life", "food"), ("宠物", "life", "pet"),
    ("家居", "life", "home"), ("宅家", "life", "home"),
    ("办公", "life", "office"), ("工作", "life", "office"),
    ("运动", "life", "sport"), ("健身", "life", "sport"), ("跑步", "life", "sport"),
    # ---- 事件 ----
    ("旅行", "event", "travel"), ("旅游", "event", "travel"), ("出游", "event", "travel"),
    ("节日", "event", "festival"), ("春节", "event", "festival"), ("中秋", "event", "festival"),
    ("庆祝", "event", "celebration"), ("生日", "event", "celebration"),
    ("演出", "event", "performance"), ("表演", "event", "performance"),
    ("音乐会", "event", "performance"), ("演唱会", "event", "performance"),
    ("毕业", "event", "graduation"),
    # ---- 动物（猫狗同时归 life.pet 与 animal，双场景合理）----
    ("动物", "animal", None), ("野生动物", "animal", "wildlife"),
    ("鸟", "animal", "bird"), ("飞鸟", "animal", "bird"),
    ("昆虫", "animal", "insect"), ("蝴蝶", "animal", "insect"),
    ("海豚", "animal", "marine"), ("鲸鱼", "animal", "marine"),
    ("猫", "animal", "cat"), ("狗", "animal", "dog"),
    # ---- 建筑 ----
    ("建筑", "architecture", None), ("教堂", "architecture", "temple"),
    ("寺庙", "architecture", "temple"), ("宫殿", "architecture", "historic"),
    ("古建筑", "architecture", "historic"), ("遗址", "architecture", "historic"),
    ("桥", "architecture", "bridge"),
    # ---- 车辆 ----
    ("汽车", "vehicle", "car"), ("自行车", "vehicle", "bike"), ("骑行", "vehicle", "bike"),
    ("船", "vehicle", "boat"), ("游艇", "vehicle", "boat"),
    ("飞机", "vehicle", "plane"), ("高铁", "vehicle", "train"),
    ("火车", "vehicle", "train"), ("列车", "vehicle", "train"),
    # ---- 艺术 ----
    ("艺术", "art", None), ("画作", "art", "painting"), ("油画", "art", "painting"),
    ("画", "art", "painting"), ("雕塑", "art", "sculpture"),
    ("涂鸦", "art", "graffiti"), ("设计", "art", "design"), ("手工", "art", "craft"),
]


def has_scene_content(scene: SceneTags | dict[str, Any] | None) -> bool:
    """判断场景结果是否有可写回内容（主场景/大场景/小场景/视觉属性任一非空）。"""
    if scene is None:
        return False
    if isinstance(scene, SceneTags):
        return bool(scene.primary_scene or scene.scenes or scene.sub_scenes) or bool(
            scene.visual_attrs
        )
    return bool(
        scene.get("primary_scene")
        or scene.get("scenes")
        or scene.get("sub_scenes")
        or scene.get("visual_attrs")
    )


def infer_scene_from_query(query: str, dictionary: SceneDict) -> SceneTags | None:
    """从查询词推断场景：提示词表 + 词典中文 label 的子串匹配。

    返回 None 表示查询无法推断（交由默认兜底）。
    """
    q = (query or "").strip().lower()
    if not q:
        return None

    scenes: list[str] = []
    sub_scenes: list[str] = []
    confidence: dict[str, float] = {}

    def _add(scene: str | None, sub: str | None) -> None:
        if sub:
            if dictionary.is_valid_sub_scene(sub) and sub not in sub_scenes:
                sub_scenes.append(sub)
                confidence[sub] = _QUERY_CONFIDENCE
            parent = dictionary.parent_of(sub)
            if parent is not None:
                scene = scene or parent
        if scene and dictionary.is_valid_scene(scene) and scene not in scenes:
            scenes.append(scene)
            confidence[scene] = _QUERY_CONFIDENCE

    for hint, scene, sub in _QUERY_SCENE_HINTS:
        if hint in q:
            _add(scene, sub)

    # 词典中文 label 兜底匹配（如查询直接包含"自然""生活"等）。
    if not scenes:
        for token, scene_key in dictionary.scene_alias_map.items():
            if len(token) >= 2 and any(ord(ch) > 127 for ch in token) and token in q:
                _add(scene_key, None)

    if not scenes:
        return None
    return SceneTags(
        primary_scene=scenes[0],
        scenes=scenes,
        sub_scenes=sub_scenes,
        visual_attrs={},
        confidence=confidence,
        source="inferred",
    )


def apply_fallback_scene(
    scene: SceneTags | dict[str, Any] | None,
    settings: Settings,
    query: str = "",
    dictionary: SceneDict | None = None,
) -> SceneTags | dict[str, Any] | None:
    """兜底：场景分析无结果时依次尝试查询推断 → 默认场景。

    - 已有任何场景内容时原样返回（不覆盖真实分析结果）。
    - 输入为 SceneTags 时返回 SceneTags（搜索路径）；dict/None 输入返回 dict（plugin 路径）。
    - 两级兜底都未命中（配置为空/非法）时原样返回。
    """
    if has_scene_content(scene):
        return scene

    is_dict = not isinstance(scene, SceneTags)
    try:
        dictionary = dictionary or get_scene_dict()
    except Exception as exc:
        logger.warning("场景兜底：词典加载失败，跳过兜底: %s", exc)
        return scene

    # 1. 查询推断（仅搜索路径传入 query）。
    if query:
        inferred = infer_scene_from_query(query, dictionary)
        if inferred is not None:
            logger.info("场景分析无结果，由查询推断场景: %s", inferred.scenes)
            return inferred.model_dump() if is_dict else inferred

    # 2. 默认场景兜底。
    fallback = (getattr(settings, "plugin_fallback_scene", "") or "").strip()
    if not fallback:
        return scene
    if not dictionary.is_valid_scene(fallback):
        logger.warning("兜底场景 %r 不在词典白名单，跳过兜底", fallback)
        return scene
    logger.info("场景分析无结果，应用兜底场景: %s", fallback)
    result = SceneTags(
        primary_scene=fallback,
        scenes=[fallback],
        sub_scenes=[],
        visual_attrs={},
        confidence={fallback: _FALLBACK_CONFIDENCE},
        source="fallback",
    )
    return result.model_dump() if is_dict else result
