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
    dedup_removed: int = Field(default=0, description="去重移除的结果数量")
    image_query_used: bool = Field(default=False, description="是否使用了以图搜图增强")
    applied_negations: list[str] = Field(
        default_factory=list, description="本次搜索实际生效的否定词列表"
    )
    applied_time_range: dict[str, Any] | None = Field(
        default=None,
        description="本次搜索生效的时间范围: {date_from, date_to, source}",
    )



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

class FeedbackResponse(BaseModel):
    """搜索反馈响应。"""

    recorded: bool = Field(default=True, description="是否成功记录")
    degraded: bool = Field(default=False, description="是否发生降级")
    warnings: list[str] = Field(default_factory=list)


class SuggestResponse(BaseModel):
    """搜索建议响应。"""

    suggestions: list[str] = Field(default_factory=list, description="建议词列表")
    degraded: bool = Field(default=False, description="是否发生降级")
    warnings: list[str] = Field(default_factory=list)


class CorrectResponse(BaseModel):
    """搜索纠错响应。"""

    corrected: str | None = Field(default=None, description="纠正后的关键词，无匹配时为 null")
    confidence: float = Field(default=0.0, description="纠错置信度 [0,1]")
    applied: bool = Field(default=False, description="是否实际应用了纠错")
    degraded: bool = Field(default=False, description="是否发生降级")
    warnings: list[str] = Field(default_factory=list)

class Album(BaseModel):
    """自动相册。"""

    album_id: str = Field(description="相册唯一标识")
    title: str = Field(description="相册标题（场景 + 月份）")
    scene: str = Field(description="大场景 key")
    cover_asset_id: str = Field(description="封面资产 ID")
    asset_ids: list[str] = Field(default_factory=list, description="相册内资产 ID 列表")
    start_date: str = Field(description="相册起始时间（ISO 格式）")
    end_date: str = Field(description="相册结束时间（ISO 格式）")
    count: int = Field(ge=0, description="相册内资产数量")


class AlbumListResponse(BaseModel):
    """自动相册列表响应。"""

    total_albums: int = Field(ge=0, description="相册总数")
    degraded: bool = Field(default=False, description="是否发生降级")
    warnings: list[str] = Field(default_factory=list)
    albums: list[Album] = Field(default_factory=list)
    cached: bool = Field(default=False, description="是否命中缓存")
