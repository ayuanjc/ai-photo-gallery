"""管理接口：项目功能开关及管理。

- GET  /admin/api/features  所有功能开关（含元数据）
- PUT  /admin/api/features  批量修改开关
- GET  /admin/api/status    系统状态（版本/Immich 连通性/缓存后端/运行时长）
- POST /admin/api/verify    校验管理员密钥（管理页面登录）

鉴权：未配置 admin_api_key 且 APP_ENV=development 时放行；否则请求头需携带
x-admin-key（hmac.compare_digest 比较）。
"""
from __future__ import annotations

import hmac

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ...core.config import get_settings
from ...core.errors import AppError, ErrorCode, UnauthorizedError
from ...core.runtime_config import (
    FEATURES_META,
    get_runtime_config,
    runtime_enabled,
)

router = APIRouter(tags=["admin"])

_STARTED_AT = time.time()


def _verify_admin_key(request: Request) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        if settings.app_env == "production":
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "生产环境必须配置 ADMIN_API_KEY",
            )
        return
    provided = request.headers.get("x-admin-key")
    if not provided or not hmac.compare_digest(provided, settings.admin_api_key):
        raise UnauthorizedError()


class FeatureUpdates(BaseModel):
    updates: dict[str, bool] = Field(..., min_length=1)


class VerifyRequest(BaseModel):
    admin_key: str = Field(..., min_length=1)


@router.get("/admin/api/features")
async def list_features(request: Request) -> dict:
    _verify_admin_key(request)
    return {"features": get_runtime_config().snapshot()}


@router.put("/admin/api/features")
async def update_features(body: FeatureUpdates, request: Request) -> dict:
    _verify_admin_key(request)
    config = get_runtime_config()
    try:
        await config.set_many(body.updates)
    except KeyError as exc:
        raise AppError(
            ErrorCode.INVALID_PARAMS,
            f"未知的功能开关: {exc}",
        ) from exc
    return {"updated": True, "features": config.snapshot()}


@router.post("/admin/api/verify")
async def verify_key(body: VerifyRequest) -> dict:
    settings = get_settings()
    expected = settings.admin_api_key
    if not expected:
        if settings.app_env == "production":
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "生产环境必须配置 ADMIN_API_KEY",
            )
        return {"valid": True, "mode": "no-auth"}
    valid = hmac.compare_digest(body.admin_key, expected)
    if not valid:
        raise UnauthorizedError()
    return {"valid": True, "mode": "key-auth"}


@router.get("/admin/api/status")
async def system_status(request: Request) -> dict:
    _verify_admin_key(request)
    settings = get_settings()
    features = get_runtime_config().snapshot()
    enabled_count = sum(1 for f in features.values() if f["enabled"])

    cache_backend = type(request.app.state.cache).__name__ if getattr(request.app.state, "cache", None) else "unknown"
    immich_ok = False
    try:
        resp = await request.app.state.immich_client._request_with_retry(
            "GET", "/api/server/ping"
        )
        immich_ok = resp.status_code == 200
    except Exception:
        immich_ok = False

    return {
        "app_env": settings.app_env,
        "version": request.app.version,
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        "immich_base_url": settings.immich_base_url,
        "immich_connected": immich_ok,
        "cache_backend": cache_backend,
        "features_enabled": enabled_count,
        "features_total": len(features),
    }


def feature_gate(key: str):
    """路由级功能开关依赖：开关关闭时返回 503。"""

    async def _gate() -> None:
        from ...core.config import get_settings as _gs

        settings = _gs()
        settings_default = {
            "ai_enabled": True,
            "feedback_enabled": True,
            "suggest_enabled": True,
            "correct_enabled": True,
            "stats_enabled": getattr(settings, "stats_enabled", True),
        }.get(key, True)
        if not runtime_enabled(key, settings_default):
            raise AppError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"功能未启用（{FEATURES_META.get(key, {}).get('name', key)}），可在管理页面开启",
            )

    return _gate