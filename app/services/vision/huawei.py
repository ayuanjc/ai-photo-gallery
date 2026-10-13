"""华为云图像识别（Image）适配器（experimental，默认关闭）。

仅提供接口骨架与鉴权占位，便于后续无缝接入官方 SDK 或鉴权 token 获取流程。
实际使用时补全：
    1. IAM 获取临时 AK/SK token；
    2. 调用 /v1.0/image/tagging 等图像识别 API；
    3. 将返回的 tags 映射为 VisionResult。

当前状态：experimental — 调用时抛 VisionUnavailableError，不返回空结果。
"""
from __future__ import annotations

import logging

from ...core.config import Settings
from ...core.errors import VisionUnavailableError
from .base import VisionProvider, VisionResult

logger = logging.getLogger(__name__)


class HuaweiVisionProvider(VisionProvider):
    name = "huawei"
    experimental = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        logger.warning("HuaweiVisionProvider 为 experimental 状态，调用时将返回 502")

    async def analyze(self, image_url: str) -> VisionResult:
        raise VisionUnavailableError(
            "华为云图像识别尚未实现（experimental），请在配置中关闭 HUAWEI_ENABLED 或切换 provider"
        )
