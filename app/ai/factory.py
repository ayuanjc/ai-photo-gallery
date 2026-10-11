"""AI 分析器工厂：根据配置返回对应实现。"""
from __future__ import annotations

from ..core.config import Settings
from ..services.immich import ImmichClient
from .base import ImageAnalyzer
from .huawei_analyzer import HuaweiFaceAnalyzer
from .immich_analyzer import ImmichAnalyzer

PROVIDERS = ("immich", "huawei")


def build_ai_analyzer(settings: Settings, immich: ImmichClient) -> ImageAnalyzer:
    """根据 AI_ANALYZER_PROVIDER 构建分析器实例（默认 immich）。"""
    provider = settings.ai_analyzer_provider.strip().lower()
    if provider == "immich":
        return ImmichAnalyzer(settings, immich)
    if provider == "huawei":
        return HuaweiFaceAnalyzer(settings)
    raise ValueError(f"未知的 AI 分析 provider: {provider}（可选: {', '.join(PROVIDERS)}）")