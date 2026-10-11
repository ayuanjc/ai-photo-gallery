"""AI 分析路由。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from ...ai.base import AnalysisResult, ImageAnalyzer
from ...models.ai import AIAnalyzeRequest
from ..deps import get_ai_analyzer, verify_api_key

router = APIRouter(tags=["ai"], dependencies=[Depends(verify_api_key)])

_MAX_CONCURRENT = 10


@router.post(
    "/ai/analyze",
    response_model=list[AnalysisResult],
    summary="AI 分析（同一个人 vs 相似图片）",
    description="给定参考图与候选图列表，返回每张候选图的同人/相似/无关判定，与请求顺序一一对应。",
)
async def analyze_images(
    request: AIAnalyzeRequest,
    analyzer: ImageAnalyzer = Depends(get_ai_analyzer),
) -> list[AnalysisResult]:
    reference = str(request.reference_url)
    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _one(candidate) -> AnalysisResult:
        async with sem:
            return await analyzer.analyze(reference, str(candidate))

    return await asyncio.gather(*(_one(c) for c in request.candidate_urls))