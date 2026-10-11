"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Request

from ..core.config import Settings, get_settings
from ..core.errors import VisionError
from ..services.face_service import FaceService
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


def get_face_service(request: Request) -> FaceService:
    service = getattr(request.app.state, "face_service", None)
    if service is None:
        raise VisionError("人脸分类功能未启用，请配置 HUAWEI_FACE_ENABLED=true 及 AK/SK/PROJECT_ID")
    return service