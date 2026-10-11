"""AI 分析请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class AIAnalyzeRequest(BaseModel):
    """AI 分析请求：给定一张参考图与一批候选图。"""

    reference_url: HttpUrl = Field(..., description="参考图片 URL")
    candidate_urls: list[HttpUrl] = Field(
        ..., min_length=1, max_length=20, description="候选图片 URL 列表（1~20 条）"
    )