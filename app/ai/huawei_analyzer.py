"""华为云人脸识别分析器（预留接入点）。

第一版不接入华为云，仅保留接口占位，后续可无缝切换。
"""
from __future__ import annotations

from ..core.config import Settings
from .base import AnalysisResult, ImageAnalyzer


class HuaweiFaceAnalyzer(ImageAnalyzer):
    """基于华为云人脸识别（FRS）的同人判定实现（未接入）。

    TODO: 接入华为云人脸识别，复用现有 app/services/vision/huawei_face.py 的
    HuaweiFaceClient，通过人脸 embedding 比对得到 person_confidence，
    替换此占位实现，使 provider=huawei 时真正可用。
    """

    name = "huawei"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def analyze(self, reference_url: str, candidate_url: str) -> AnalysisResult:
        raise NotImplementedError("华为云人脸分析器尚未实现（TODO: 接入华为云 FRS）")

    async def health_check(self) -> bool:
        raise NotImplementedError("华为云人脸分析器尚未实现（TODO: 接入华为云 FRS）")
