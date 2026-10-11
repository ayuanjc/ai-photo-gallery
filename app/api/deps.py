"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Request

from ..core.config import Settings, get_settings
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