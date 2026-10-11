"""FastAPI 应用入口。

包含：
- lifespan：创建并管理 Immich 客户端、视觉识别 provider、搜索服务的生命周期。
- 请求 ID 中间件：透传/生成 x-request-id，便于日志与监控关联。
- 全局异常处理器：将领域异常与校验异常映射为统一错误结构。
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.routes import scene, search
from .core.config import get_settings
from .core.errors import AppError, ErrorCode
from .core.logging import setup_logging
from .scene.analyzer import NullSceneInferrer, SceneAnalyzer
from .scene.dictionary import get_scene_dict
from .services.immich import ImmichClient
from .services.search_service import SearchService
from .services.vision.base import NullVisionProvider, VisionProvider
from .services.vision.huawei import HuaweiVisionProvider

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)

    immich = ImmichClient(settings)
    vision: VisionProvider = (
        HuaweiVisionProvider(settings) if settings.huawei_enabled else NullVisionProvider()
    )
    scene_dictionary = get_scene_dict()
    scene_analyzer = (
        SceneAnalyzer(
            scene_dictionary,
            inferrer=NullSceneInferrer(),
            cache_size=settings.scene_cache_size,
            infer_timeout_seconds=settings.scene_infer_timeout_seconds,
        )
        if settings.scene_enabled
        else None
    )
    app.state.immich_client = immich
    app.state.vision_provider = vision
    app.state.scene_dictionary = scene_dictionary
    app.state.scene_analyzer = scene_analyzer
    app.state.search_service = SearchService(
        settings, immich, vision, scene_analyzer=scene_analyzer
    )

    logger.info("应用启动：env=%s immich=%s", settings.app_env, settings.immich_base_url)
    try:
        yield
    finally:
        await immich.aclose()
        logger.info("应用关闭")


app = FastAPI(
    title="AI Image Semantic Search Service",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(search.router, prefix="/api/v1")
app.include_router(scene.router, prefix="/api/v1")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code.value,
                "message": exc.message,
                "request_id": _request_id(request),
                "details": exc.details,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 仅保留可序列化的核心字段，避免原始 ValueError/输入对象导致的 JSON 序列化失败
    details = [
        {
            "loc": [str(p) for p in err.get("loc", [])],
            "msg": str(err.get("msg", "")),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": ErrorCode.INVALID_PARAMS.value,
                "message": "请求参数校验失败",
                "request_id": _request_id(request),
                "details": details,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "服务内部错误",
                "request_id": _request_id(request),
                "details": None,
            }
        },
    )


@app.get("/health", tags=["system"])
async def health() -> dict:
    return {"status": "ok"}