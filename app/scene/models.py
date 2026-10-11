"""场景标签数据模型（Pydantic）。

一张图片可同时拥有多个场景属性，采用三级正交体系：
    - primary_scene: 主大场景（单选，用于决定排序权重）。
    - scenes:        大场景列表（多选）。
    - sub_scenes:    小场景列表（多选）。
    - visual_attrs:  视觉属性（如时间/人数/室内外）。
    - confidence:    各标签置信度（0~1）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SceneTags(BaseModel):
    """单图的多标签场景结果。"""

    primary_scene: str | None = Field(
        default=None, description="主大场景（单选，用于排序权重）"
    )
    scenes: list[str] = Field(default_factory=list, description="大场景（多选）")
    sub_scenes: list[str] = Field(default_factory=list, description="小场景（多选）")
    visual_attrs: dict[str, str] = Field(
        default_factory=dict, description="视觉属性，如 {'time':'night','people':'none'}"
    )
    confidence: dict[str, float] = Field(
        default_factory=dict, description="标签置信度，取值 0~1"
    )

    def has_scene(self) -> bool:
        """是否存在任何大场景或小场景标签。"""
        return bool(self.scenes or self.sub_scenes)