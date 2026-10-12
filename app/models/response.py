"""响应模型（Pydantic）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..scene.models import SceneTags


class SearchResultItem(BaseModel):
    """单条搜索结果。"""

    asset_id: str
    thumbnail_url: str
    original_url: str | None = None
    captured_at: datetime | None = Field(default=None, description="EXIF 拍摄时间（回退后）")
    captured_at_source: str = Field(default="default", description="拍摄时间来源: exif | file_created | default")
    similarity_score: float = Field(ge=0.0, le=1.0, description="语义相似度分数（归一化到 0~1）")
    time_freshness: float = Field(ge=0.0, le=1.0, description="时间新鲜度（归一化到 0~1）")
    final_score: float = Field(ge=0.0, le=1.0, description="加权融合后的最终排序分数")
    metadata: dict[str, Any] = Field(default_factory=dict, description="所选元数据字段")
    scene: SceneTags | None = Field(default=None, description="多标签场景分析结果")
    explanation: dict[str, Any] | None = Field(
        default=None, description="排序可解释性信息（为什么排这里）"
    )


class SearchResponse(BaseModel):
    """语义搜索统一响应。"""

    query: str
    total: int
    degraded: bool = Field(default=False, description="是否发生降级（如上游部分可用）")
    warnings: list[str] = Field(default_factory=list)
    results: list[SearchResultItem] = Field(default_factory=list)
    enriched_query: str | None = Field(default=None, description="经视觉识别增强后的实际查询词")
    cached: bool = Field(default=False, description="是否命中缓存")



class MultiConditionResultItem(BaseModel):
    """复合条件搜索单条结果。"""

    asset_id: str
    thumbnail_url: str
    multi_score: float = Field(description="加权综合评分 Score_multi = Σ(wi * Si)")
    matched_conditions: list[str] = Field(description="命中的条件列表")
    person_score: float = Field(default=0.0, ge=0.0, le=1.0)
    location_score: float = Field(default=0.0, ge=0.0, le=1.0)
    query_score: float = Field(default=0.0, ge=0.0, le=1.0)


class MultiConditionSearchResponse(BaseModel):
    """复合条件搜索响应。"""

    total: int
    degraded: bool = Field(default=False, description="是否触发了贝叶斯降级检索")
    degraded_conditions: list[str] = Field(
        default_factory=list,
        description="降级时放宽的条件列表（按权重从低到高排序）",
    )
    warnings: list[str] = Field(default_factory=list)
    results: list[MultiConditionResultItem] = Field(default_factory=list)
