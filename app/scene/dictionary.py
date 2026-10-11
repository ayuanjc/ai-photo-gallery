"""场景词典：从白名单文件加载场景体系，并提供白名单校验原语。

设计约定（任务原定 YAML，因运行环境缺少 PyYAML 且无法编译其 C 扩展，
改用等价的 JSON 白名单文件）：
    - 大场景：nature/city/people/life/event，标签以其 key 与中文 label 双向映射。
    - 小场景：每个大场景的 children，全局去重。
    - 视觉属性：time / people / indoor 三类，每类 2~4 个取值。

白名单原则：任何未收录的标签一律丢弃（不自由生成），由上层决定是否归入 other。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 场景词典文件与本模块同目录。
DICT_PATH: Path = Path(__file__).with_name("scene_dict.json")

# 未命中白名单时的兜底场景 key。
OTHER_SCENE = "other"


@dataclass(frozen=True)
class SceneDict:
    """已加载并解析的场景词典（只读）。"""

    scenes: dict[str, dict[str, Any]]
    visual_attrs: dict[str, list[str]]

    @property
    def scene_keys(self) -> frozenset[str]:
        return frozenset(self.scenes)

    @property
    def sub_scene_keys(self) -> frozenset[str]:
        keys: set[str] = set()
        for entry in self.scenes.values():
            keys.update(entry.get("children", []))
        return frozenset(keys)

    @property
    def scene_alias_map(self) -> dict[str, str]:
        """归一化 token -> 大场景 key 的别名映射（含中文 label）。"""
        alias: dict[str, str] = {}
        for key, entry in self.scenes.items():
            alias[_normalize(key)] = key
            label = entry.get("label")
            if isinstance(label, str) and label:
                alias[_normalize(label)] = key
        return alias

    def parent_of(self, sub_scene: str) -> str | None:
        """返回小场景所属的大场景 key，未收录返回 None。"""
        for key, entry in self.scenes.items():
            if sub_scene in entry.get("children", []):
                return key
        return None

    def is_valid_scene(self, key: str) -> bool:
        """校验是否为白名单内的大场景 key。"""
        return key in self.scenes

    def is_valid_sub_scene(self, key: str) -> bool:
        """校验是否为白名单内的小场景 key。"""
        return key in self.sub_scene_keys

    def is_valid_visual_attr(self, category: str, value: str) -> bool:
        """校验视觉属性取值是否在白名单内。"""
        return value in self.visual_attrs.get(category, [])

    def to_dict(self) -> dict[str, Any]:
        """返回可由接口直接输出的词典结构。"""
        return {"scenes": self.scenes, "visual_attrs": self.visual_attrs}


def _normalize(token: str) -> str:
    """归一化自由文本标签：小写 + 连字符/空格统一为下划线。"""
    return token.strip().lower().replace("-", "_").replace(" ", "_")


def load_scene_dict(path: Path | str | None = None) -> SceneDict:
    """加载场景词典文件并校验基本结构。"""
    p = Path(path) if path is not None else DICT_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    scenes = data.get("scenes")
    visual_attrs = data.get("visual_attrs")
    if not isinstance(scenes, dict) or not isinstance(visual_attrs, dict):
        raise ValueError("场景词典结构非法：需包含 scenes 与 visual_attrs 两个字典")
    logger.info("场景词典加载完成：%d 个大场景，%s", len(scenes), p)
    return SceneDict(scenes=scenes, visual_attrs=visual_attrs)


@lru_cache
def get_scene_dict() -> SceneDict:
    """进程级单例场景词典。"""
    return load_scene_dict()