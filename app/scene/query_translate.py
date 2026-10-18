"""中文查询 → 英文 CLIP 检索词翻译。

Immich 的 CLIP 模型（ViT-B-32__openai）以英文语料训练，中文查询的
文本 embedding 无语义区分度，smart search 的中文召回基本无效
（实测"巍峨群山"返回海边图与猫图）。本模块把查询中的中文场景
关键词映射为词典场景的英文 CLIP 提示词后再检索：

    中文查询 --(_QUERY_SCENE_HINTS / 词典中文 label 匹配)--> 场景 key
            --(SCENE_PROMPTS)--> 英文提示词 --> 英文检索查询

- 仅用于发给 Immich 的检索查询；展示/反馈/兜底推断仍用原始查询。
- 无 CJK 字符（纯英文）或无命中时原样返回：英文查询天然有效，
  无法翻译的中文透传保持既有行为。
"""
from __future__ import annotations

import logging

from .clip_inferrer import SCENE_PROMPTS
from .dictionary import SceneDict
from .fallback import _QUERY_SCENE_HINTS

logger = logging.getLogger(__name__)

# 拼接进英文查询的提示词上限（过多概念会稀释 CLIP 文本 embedding）。
_MAX_PROMPTS = 5

# CJK Unicode 区间（判断查询是否含中文）。
_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK 统一表意文字
    (0x3400, 0x4DBF),  # 扩展 A
    (0xF900, 0xFAFF),  # 兼容表意文字
)


def _has_cjk(text: str) -> bool:
    return any(
        any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text
    )


def _collect_keys(query: str, dictionary: SceneDict) -> list[str]:
    """从查询收集命中的场景 key：子场景优先（更具体），保序去重。"""
    keys: list[str] = []
    for hint, scene, sub in _QUERY_SCENE_HINTS:
        if hint in query:
            for key in (sub, scene):
                if key and key not in keys:
                    keys.append(key)
    if not keys:
        # 词典中文 label 兜底（如查询直接包含"自然""建筑"等）。
        for token, scene_key in dictionary.scene_alias_map.items():
            if (
                len(token) >= 2
                and _has_cjk(token)
                and token in query
                and scene_key not in keys
            ):
                keys.append(scene_key)
    return keys


def translate_query_for_clip(
    query: str, dictionary: SceneDict | None = None
) -> tuple[str, bool]:
    """把中文查询翻译为英文 CLIP 检索词。

    返回 (检索查询, 是否发生翻译)。无中文/无命中/词典加载失败时
    原样返回 (query, False)，不影响主链路。
    """
    q = (query or "").strip()
    if not q or not _has_cjk(q):
        return query, False

    if dictionary is None:
        try:
            from .dictionary import get_scene_dict

            dictionary = get_scene_dict()
        except Exception as exc:
            logger.warning("查询翻译：词典加载失败，透传原查询: %s", exc)
            return query, False

    keys = _collect_keys(q, dictionary)
    if not keys:
        return query, False

    prompts: list[str] = []
    for key in keys:
        entry = SCENE_PROMPTS.get(key)
        if entry and entry[0] not in prompts:
            prompts.append(entry[0])
    if not prompts:
        return query, False

    return ", ".join(prompts[:_MAX_PROMPTS]), True
