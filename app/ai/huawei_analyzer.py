"""华为云人脸识别分析器（experimental，未接入）。

当前为占位实现，调用时返回明确的 502 错误，不产生未捕获的 NotImplementedError。
"""
from __future__ import annotations

import logging

from ..core.config import Settings
from ..core.errors import AIAnalyzerUnavailableError
from .base import AnalysisResult, ImageAnalyzer

logger = logging.getLogger(__name__)


class HuaweiFaceAnalyzer(ImageAnalyzer):
    """基于华为云人脸识别（FRS）的同人判定实现（experimental）。"""

    name = "huawei"
    experimental = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        logger.warning("HuaweiFaceAnalyzer 为 experimental 状态，调用时将返回 502")

    async def analyze(self, reference_url: str, candidate_url: str) -> AnalysisResult:
        raise AIAnalyzerUnavailableError(
            "华为云人脸分析器尚未实现（experimental），请设置 AI_ANALYZER_PROVIDER=immich"
        )

    async def health_check(self) -> bool:
        return False
