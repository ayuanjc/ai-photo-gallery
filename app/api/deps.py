"""FastAPI 依赖注入。"""
from __future__ import annotations

import hmac

from fastapi import Depends, Request

from ..ai.base import ImageAnalyzer
from ..core.config import Settings, get_settings
from ..core.errors import AppError, ErrorCode, ServiceUnavailableError, UnauthorizedError
from ..core.rate_limit import TokenBucketRateLimiter
from ..scene.analyzer import SceneAnalyzer
from ..scene.dictionary import SceneDict
from ..services.face_service import FaceService
from ..services.immich import ImmichClient
from ..services.multi_condition_service import MultiConditionSearchService
from ..services.search_service import SearchService
from ..services.vision.base import VisionProvider


def get_settings_dep() -> Settings:
    return get_settings()


async def verify_api_key(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """搜索接口 API Key 鉴权。

    未配置 search_api_key（空字符串）时直接放行；配置后校验请求头 x-api-key，
    缺失或不匹配抛 UnauthorizedError（映射 401，走全局异常处理器统一格式）。
    """
    if not settings.search_api_key:
        return
    provided = request.headers.get("x-api-key")
    if not provided or not hmac.compare_digest(provided, settings.search_api_key):
        raise UnauthorizedError()


def _get_client_id(request: Request) -> str:
    """提取限流客户端标识：优先 x-api-key，其次真实 IP，兜底 testclient。"""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"key:{api_key}"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


async def rate_limit_guard(request: Request) -> None:
    """搜索接口令牌桶限流。

    从 app.state.rate_limiter 获取限流器，按客户端标识（API Key 或 IP）检查令牌。
    超限时抛 AppError(RATE_LIMITED)（映射 429），响应头带 X-RateLimit-Remaining。
    限流器不存在时直接放行（降级）。
    """
    limiter: TokenBucketRateLimiter | None = getattr(
        request.app.state, "rate_limiter", None
    )
    if limiter is None:
        return
    client_id = _get_client_id(request)
    allowed, remaining = await limiter.allow(client_id)
    if not allowed:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "请求过于频繁，请稍后重试",
            headers={"X-RateLimit-Remaining": str(int(remaining))},
        )


def get_immich_client(request: Request) -> ImmichClient:
    return request.app.state.immich_client


def get_vision_provider(request: Request) -> VisionProvider:
    return request.app.state.vision_provider


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


def get_multi_condition_service(request: Request) -> MultiConditionSearchService:
    return request.app.state.multi_condition_service


def get_scene_analyzer(request: Request) -> SceneAnalyzer | None:
    """返回场景分析器（未启用时可能为 None）。"""
    return getattr(request.app.state, "scene_analyzer", None)


def get_scene_dictionary(request: Request) -> SceneDict:
    """返回进程级场景词典（只读）。"""
    return request.app.state.scene_dictionary


def get_face_service(request: Request) -> FaceService:
    service = getattr(request.app.state, "face_service", None)
    if service is None:
        raise ServiceUnavailableError("人脸分类功能未启用，请配置 HUAWEI_FACE_ENABLED=true 及 AK/SK/PROJECT_ID")
    return service


def get_ai_analyzer(request: Request) -> ImageAnalyzer:
    """返回 AI 图像分析器（同一个人 vs 相似图片）。"""
    return request.app.state.ai_analyzer
