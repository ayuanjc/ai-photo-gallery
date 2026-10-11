"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Request

from ..core.config import Settings, get_settings
from ..scene.analyzer import SceneAnalyzer
from ..scene.dictionary import SceneDict
from ..services.immich import ImmichClient
from ..services.search_service import SearchService
from ..services.vision.base import VisionProvider


def get_settings_dep() -> Settings:
    return get_settings()


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