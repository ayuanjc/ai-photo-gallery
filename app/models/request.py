"""请求模型（Pydantic）与字段校验。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

# 允许客户端请求返回的图片元数据字段白名单，防止任意字段注入。
ALLOWED_METADATA_FIELDS = frozenset(
    {
        "width",
        "height",
        "format",
        "latitude",
        "longitude",
        "city",
        "state",
        "country",
        "make",
        "model",
        "lensModel",
        "iso",
        "exposureTime",
        "fNumber",
        "focalLength",
        "orientation",
        "projectionType",
    }
)

DEFAULT_METADATA_FIELDS = [
    "width",
    "height",
    "format",
    "latitude",
    "longitude",
    "city",
    "country",
]


class SearchRequest(BaseModel):
    """语义搜索请求。

    - query: 自然语言搜索关键词（必填，去空白后非空）。
    - asset_ids: Immich 图库中已有的资产 ID 列表（必填、非空），用于以图搜图。
    - limit: 期望返回的结果数量。
    - time_weight / similarity_weight: 可选的单次请求权重覆盖（0~1）。
    - include_metadata_fields: 希望返回的元数据字段（白名单校验）。
    - include_original_url: 是否返回原始图片 URL。
    """

    query: str = Field(..., description="自然语言搜索关键词")
    asset_ids: list[str] = Field(..., min_length=1, max_length=200, description="Immich 资产 ID 列表")
    limit: int = Field(default=50, ge=1, le=200, description="返回结果数量上限")
    time_weight: float | None = Field(default=None, ge=0.0, le=1.0, description="时间权重 α")
    similarity_weight: float | None = Field(default=None, ge=0.0, le=1.0, description="相似度权重 β")
    include_metadata_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_METADATA_FIELDS))
    include_original_url: bool = True

    @field_validator("asset_ids")
    @classmethod
    def _validate_asset_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for aid in value:
            if not aid or not aid.strip():
                raise ValueError("asset_id 不能为空字符串")
            if len(aid) > 128:
                raise ValueError(f"asset_id 长度不能超过 128 字符: {aid[:20]}...")
            if aid in seen:
                raise ValueError(f"asset_id 重复: {aid}")
            seen.add(aid)
        return value

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("查询关键词不能为空")
        if len(value) > 500:
            raise ValueError("查询关键词长度不能超过 500 字符")
        return value

    @field_validator("include_metadata_fields")
    @classmethod
    def _validate_metadata_fields(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALLOWED_METADATA_FIELDS)
        if unknown:
            raise ValueError(f"不支持的元数据字段: {', '.join(unknown)}")
        return value

class MultiConditionSearchRequest(BaseModel):
    """复合条件搜索请求。

    - person_id: Immich 人物 ID（可选，按人物过滤候选资产）。
    - location: 地点关键词（可选，按 EXIF 城市/国家过滤）。
    - query: 语义搜索关键词（可选，调用 Immich 语义搜索）。
    - limit: 返回结果数量上限。

    至少提供一个条件；三条件同时命中为空时触发贝叶斯降级检索。
    """

    person_id: str | None = Field(default=None, description="Immich 人物 ID")
    location: str | None = Field(default=None, description="地点关键词（城市/国家）")
    query: str | None = Field(default=None, description="语义搜索关键词")
    limit: int = Field(default=50, ge=1, le=200, description="返回结果数量上限")

    @model_validator(mode="after")
    def _at_least_one_condition(self) -> MultiConditionSearchRequest:
        if not any([self.person_id, self.location, self.query]):
            raise ValueError("至少需要提供一个搜索条件（person_id, location, query）")
        return self

    @field_validator("query")
    @classmethod
    def _query_length_limit(cls, value: str | None) -> str | None:
        if value and len(value) > 500:
            raise ValueError("查询关键词长度不能超过 500 字符")
        return value
