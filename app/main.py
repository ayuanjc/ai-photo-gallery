"""FastAPI 应用入口。

包含：
- lifespan：创建并管理 Immich 客户端、视觉识别 provider、搜索服务的生命周期。
- 请求 ID 中间件：透传/生成 x-request-id，便于日志与监控关联。
- 全局异常处理器：将领域异常与校验异常映射为统一错误结构。
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .ai.factory import build_ai_analyzer
from .api.routes import ai, admin, assets, batch_import, faces, plugin, ratings, scene, search, stats, watermark
from .core.cache import get_cache
from .core.config import get_settings
from .core.errors import AppError, ErrorCode
from .core.logging import setup_logging
from .core.rate_limit import TokenBucketRateLimiter
from .core.redis_client import RedisClient
from .db import close_db, init_db
from .scene.analyzer import HttpSceneInferrer, NullSceneInferrer, SceneAnalyzer
from .scene.dictionary import get_scene_dict
from .services.face_service import FaceService
from .services.feedback_service import init_feedback_service
from .services.immich import ImmichClient
from .services.immich_capabilities import ImmichCapabilities
from .services.immich_writeback import ImmichWriteBack
from .services.scene_writeback import SceneWriteBack
from .services.album_writeback import AlbumWriteBack
from .services.description_writeback import DescriptionWriteBack
from .services.image_proxy import ImageProxyService
from .services.incremental_sync import IncrementalSync
from .services.workflow_manager import WorkflowManager
from .services.writeback_manager import WriteBackManager

from .services.immich_uploader import ImmichAssetUploader
from .services.importer import BatchImporter
from .services.metadata import MetadataExtractor
from .services.multi_condition_service import MultiConditionSearchService
from .services.rating_service import RatingService
from .services.search_service import SearchService
from .services.stats_provider import StatsProvider
from .services.vision.base import NullVisionProvider, VisionProvider
from .services.vision.huawei import HuaweiVisionProvider
from .services.vision.huawei_face import HuaweiFaceClient
from .services.watermark_processor import BatchWatermarkProcessor

logger = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,128}$")

_SENSITIVE_PATTERNS = [
    (re.compile(r'://[^:\s]+:[^@\s]+@'), '://***@'),
    (re.compile(r'(password|secret|token|api[_-]?key|credential)\s*[=:]\s*\S+', re.IGNORECASE), r'\1=***'),
    (re.compile(r'["\']?(password|secret|token|api[_-]?key|credential)["\']?\s*[:=]\s*["\']?([^"\'\s,}]+)', re.IGNORECASE), r'\1=***'),
]


def _sanitize_message(message: str) -> str:
    for pattern, replacement in _SENSITIVE_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)

    immich = ImmichClient(settings)

    try:
        capabilities = await ImmichCapabilities.probe(immich)
    except Exception:
        logger.exception("Immich 能力探测失败，使用空能力集降级")
        capabilities = ImmichCapabilities()
    app.state.capabilities = capabilities

    vision: VisionProvider = (
        HuaweiVisionProvider(settings) if settings.huawei_enabled else NullVisionProvider()
    )
    scene_dictionary = get_scene_dict()

    # Redis 缓存后端：启动时连接（失败不阻塞），先于 scene_analyzer / search_service 创建。
    redis_client = RedisClient(settings)
    await redis_client.connect()
    app.state.redis_client = redis_client
    cache = await get_cache(settings, redis_client)
    app.state.cache = cache

    scene_inferrer = (
        HttpSceneInferrer(settings.scene_inference_endpoint)
        if settings.scene_inference_endpoint
        else NullSceneInferrer()
    )
    scene_analyzer = (
        SceneAnalyzer(
            scene_dictionary,
            inferrer=scene_inferrer,
            cache_size=settings.scene_cache_size,
            cache_halflife_seconds=settings.scene_cache_halflife_days * 86400,
            infer_timeout_seconds=settings.scene_infer_timeout_seconds,
            cache=cache,
            cache_ttl_seconds=settings.scene_cache_ttl_seconds,
        )
        if settings.scene_enabled
        else None
    )
    face_client = HuaweiFaceClient(settings) if settings.huawei_face_enabled else None
    app.state.immich_client = immich
    app.state.vision_provider = vision
    app.state.scene_dictionary = scene_dictionary
    app.state.scene_analyzer = scene_analyzer
    app.state.face_client = face_client
    app.state.face_service = FaceService(settings, face_client) if face_client else None
    app.state.ai_analyzer = build_ai_analyzer(settings, immich)
    app.state.search_service = SearchService(
        settings,
        immich,
        vision,
        scene_analyzer=scene_analyzer,
        cache=cache,
        cache_ttl_seconds=settings.search_cache_ttl_seconds,
        capabilities=capabilities,
    )
    app.state.multi_condition_service = MultiConditionSearchService(settings, immich)


    # 搜索反馈闭环
    init_feedback_service(cache, settings)

    # Immich 写回：场景标签 → Immich tags, AI 相册
    writeback = ImmichWriteBack(immich, settings)
    app.state.immich_writeback = writeback
    app.state.scene_writeback = SceneWriteBack(writeback, settings)
    app.state.album_writeback = AlbumWriteBack(writeback, settings)
    app.state.description_writeback = DescriptionWriteBack(writeback, settings)

    # 图片代理：前端通过本服务访问 Immich 缩略图/原图
    app.state.image_proxy_service = ImageProxyService(immich, settings)

    # 增量水位同步：按 updatedAt 增量拉取 Immich 资产变更
    app.state.incremental_sync = IncrementalSync(immich, settings)

    # 写回管理：审计日志 + 一键回滚
    app.state.writeback_manager = WriteBackManager(writeback, settings)

    # 工作流管理：Immich 原生工作流创建/管理
    app.state.workflow_manager = WorkflowManager(immich, settings)

    # ML 服务层：场景推理 + 向量索引 + 模型管理
    from .services.ml_scene_inferrer import create_ml_inferrer
    from .services.vector_index import VectorIndex
    from .services.model_manager import ModelManager
    app.state.ml_inferrer = create_ml_inferrer(settings)
    app.state.vector_index = VectorIndex()
    app.state.model_manager = ModelManager()

    # 批量导入：元数据提取 → 上传 Immich
    metadata_extractor = MetadataExtractor(settings.default_datetime_iso)
    immich_uploader = ImmichAssetUploader(settings)
    app.state.immich_uploader = immich_uploader
    app.state.batch_importer = BatchImporter(settings, metadata_extractor, immich_uploader)

    # 水印管理：模板解析 + 子进程批量渲染
    app.state.watermark_processor = BatchWatermarkProcessor(settings)

    # 令牌桶限流器：基于 Redis 原子操作，不可用时降级放行
    app.state.rate_limiter = TokenBucketRateLimiter(
        settings, redis=redis_client.redis
    )

    # 评分系统：初始化数据库 + 创建仓库 + 创建服务
    app.state.rating_service = None
    if settings.rating_enabled:
        try:
            from .repositories.sql_rating import SqlRatingRepository
            session_factory = await init_db(settings.database_url)
            repository = SqlRatingRepository(session_factory)
            app.state.rating_service = RatingService(repository)
            logger.info("评分系统初始化完成: %s", settings.database_url)
        except Exception as exc:
            logger.warning("评分系统初始化失败，降级禁用评分功能: %s", exc)
            app.state.rating_service = None


    logger.info("应用启动：env=%s immich=%s", settings.app_env, settings.immich_base_url)
    try:
        yield
    finally:
        if scene_analyzer is not None:
            await scene_analyzer.aclose()
        if face_client is not None:
            await face_client.aclose()
        await immich_uploader.aclose()
        await immich.aclose()
        app.state.watermark_processor.close()
        await redis_client.aclose()
        if getattr(app.state, "rating_service", None) is not None:
            await close_db()
        logger.info("应用关闭")


app = FastAPI(
    title="AI 图像语义搜索微服务",
    description="基于 FastAPI 的 AI 图像语义搜索微服务，对接 Immich 图库与华为云 FRS 人脸识别。",
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "search", "description": "搜索接口：语义搜索、复合条件搜索、反馈、建议、纠错"},
        {"name": "scene", "description": "场景词典：多标签场景体系（只读）"},
        {"name": "faces", "description": "人脸分类：基于华为云 FRS 的人脸检测与聚类"},
        {"name": "ai", "description": "AI 分析：同人/相似图片判定"},
        {"name": "stats", "description": "统计大屏：地图足迹、EXIF 分布、器材偏好"},
        {"name": "import", "description": "批量导入：本地图片 EXIF 提取并上传 Immich"},
        {"name": "watermark", "description": "水印管理：预置模板、授权类型、批量添加水印"},
        {"name": "rating", "description": "照片评分：创建、更新、查询、删除、批量获取、统计"},
        {"name": "assets", "description": "资产代理：缩略图/原图代理获取"},
        {"name": "plugin", "description": "Immich 插件/工作流 hook 回调"},
        {"name": "system", "description": "系统接口：健康检查"},
    ],
)

app.include_router(search.router, prefix="/api/v1")
app.include_router(scene.router, prefix="/api/v1")
app.include_router(faces.router, prefix="/api/v1")
app.include_router(ai.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")
app.include_router(assets.router, prefix="/api/v1")
app.include_router(plugin.router, prefix="/api/v1")
app.include_router(batch_import.router, prefix="/api/v1")
app.include_router(watermark.router, prefix="/api/v1")
app.include_router(ratings.router, prefix="/api/v1")
app.include_router(admin.router)

_STATIC_DIR = str(Path(__file__).parent / "static")

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    """管理页面：项目功能开关及系统状态。"""
    return FileResponse(str(Path(_STATIC_DIR) / "admin.html"))


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    if not _REQUEST_ID_RE.match(request_id):
        request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
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
                "message": _sanitize_message(exc.message),
                "request_id": _request_id(request),
                "details": exc.details,
            }
        },
        headers=exc.headers,
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

@app.get("/livez", tags=["system"])
async def livez() -> dict:
    return {"status": "ok"}


@app.get("/readyz", tags=["system"])
async def readyz(request: Request) -> dict:
    checks: dict[str, bool] = {}
    try:
        resp = await request.app.state.immich_client._request_with_retry(
            "GET", "/api/server/ping"
        )
        checks["immich"] = resp.status_code == 200
    except Exception:
        checks["immich"] = False
    try:
        cache = getattr(request.app.state, "cache", None)
        checks["cache"] = cache is not None
    except Exception:
        checks["cache"] = False
    all_ok = all(checks.values())
    return {"status": "ok" if all_ok else "not_ready", "checks": checks}

