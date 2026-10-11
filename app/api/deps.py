"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Depends, Request

from ..ai.base import ImageAnalyzer
from ..core.config import Settings, get_settings
from ..core.errors import UnauthorizedError, VisionError
from ..scene.analyzer import SceneAnalyzer
from ..scene.dictionary import SceneDict
from ..services.face_service import FaceService
from ..services.immich import ImmichClient
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
    if provided != settings.search_api_key:
        raise UnauthorizedError()


def get_immich_client(request: Request) -> ImmichClient:
    return request.app.state.immich_client


def get_vision_provider(request: Request) -> VisionProvider:
    return request.app.state.vision_provider


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


def get_scene_analyzer(request: Request) -> SceneAnalyzer | None:
    """返回场景分析器（未启用时可能为 None）。"""
    return getattr(request.app.state, "scene_analyzer", None)


def get_scene_dictionary(request: Request) -> SceneDict:
    """返回进程级场景词典（只读）。"""
    return request.app.state.scene_dictionary


def get_face_service(request: Request) -> FaceService:
    service = getattr(request.app.state, "face_service", None)
    if service is None:
        raise VisionError("人脸分类功能未启用，请配置 HUAWEI_FACE_ENABLED=true 及 AK/SK/PROJECT_ID")
    return service


def get_ai_analyzer(request: Request) -> ImageAnalyzer:
    """返回 AI 图像分析器（同一个人 vs 相似图片）。"""
    return request.app.state.ai_analyzer