"""华为云图像识别（Image）适配器（预留扩展，默认关闭）。

仅提供接口骨架与鉴权占位，便于后续无缝接入官方 SDK 或鉴权 token 获取流程。
实际使用时补全：
    1. IAM 获取临时 AK/SK token；
    2. 调用 /v1.0/image/tagging 等图像识别 API；
    3. 将返回的 tags 映射为 VisionResult。
"""
from __future__ import annotations

import logging

from ...core.config import Settings
from .base import VisionProvider, VisionResult

logger = logging.getLogger(__name__)


class HuaweiVisionProvider(VisionProvider):
    name = "huawei"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def analyze(self, image_url: str) -> VisionResult:
        # 占位实现：真实场景中应调用华为云图像识别服务。
        # 此处只做日志与空结果返回，保证关闭/未实现时不阻塞主流程。
        logger.debug("HuaweiVisionProvider.analyze 尚未实现，image_url=%s", image_url)
        return VisionResult(raw={"provider": self.name, "stub": True})