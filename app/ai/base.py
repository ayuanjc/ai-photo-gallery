"""AI 图像分析抽象层。

把「同一个人」与「相似图片」拆成两个独立判定维度，避免误分类。

分析结果包含三个互不混淆的字段：
    - same_person:       是否同一个人
    - person_confidence: 人脸判定置信度（无脸时为 0.0）
    - visual_similarity: 画面整体相似度
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["same_person", "similar_image", "unrelated"]


class AnalysisResult(BaseModel):
    """单张候选图相对参考图的分析结果。"""

    same_person: bool = Field(description="是否与参考图是同一人")
    person_confidence: float = Field(ge=0.0, le=1.0, description="人脸判定置信度，无脸为 0.0")
    visual_similarity: float = Field(ge=0.0, le=1.0, description="画面整体相似度")
    category: Category = Field(description="分类结果")
    reason: str = Field(description="判定说明")


def categorize(
    same_person: bool,
    person_confidence: float,
    visual_similarity: float,
    person_threshold: float,
    similarity_threshold: float,
) -> tuple[Category, str]:
    """按优先级分类：同一人 > 相似图片 > 无关。"""
    if same_person and person_confidence >= person_threshold:
        return "same_person", "人脸判定为同一人"
    if visual_similarity >= similarity_threshold:
        return "similar_image", "画面整体相似"
    return "unrelated", "既非同一人，画面也不相似"


class ImageAnalyzer(ABC):
    """图像分析器抽象基类。"""

    name: str = "abstract"

    @abstractmethod
    async def analyze(self, reference_url: str, candidate_url: str) -> AnalysisResult:
        """分析候选图相对参考图的关系。"""

    @abstractmethod
    async def health_check(self) -> bool:
        """返回分析器当前是否可用。"""