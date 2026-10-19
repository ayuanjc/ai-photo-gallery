"""AI 分析请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator

from ..utils.url_safety import validate_url_or_raise


class AIAnalyzeRequest(BaseModel):
    """AI 分析请求：给定一张参考图与一批候选图。"""

    reference_url: HttpUrl = Field(..., description="参考图片 URL")
    candidate_urls: list[HttpUrl] = Field(
        ..., min_length=1, max_length=20, description="候选图片 URL 列表（1~20 条）"
    )

    @field_validator("reference_url")
    @classmethod
    def _validate_reference_url(cls, v: HttpUrl) -> HttpUrl:
        validate_url_or_raise(str(v), resolve_dns=False)
        return v

    @field_validator("candidate_urls")
    @classmethod
    def _validate_candidate_urls(cls, v: list[HttpUrl]) -> list[HttpUrl]:
        for url in v:
            validate_url_or_raise(str(url), resolve_dns=False)
        return v
