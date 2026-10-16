"""管理接口：项目功能开关及管理。

- GET  /admin/api/features  所有功能开关（含元数据）
- PUT  /admin/api/features  批量修改开关
- GET  /admin/api/status    系统状态（版本/Immich 连通性/缓存后端/运行时长）
- POST /admin/api/verify    校验管理员密钥（管理页面登录）
- GET  /admin/api/settings  获取配置（敏感值脱敏）
- PUT  /admin/api/settings  更新配置（写入 .env 文件，需重启生效）

鉴权：未配置 admin_api_key 且 APP_ENV=development 时放行；否则请求头需携带
x-admin-key（hmac.compare_digest 比较）。
"""
from __future__ import annotations

import hmac
import time
from pathlib import Path

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
                ErrorCode.SERVICE_UNAVAILABLE,
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


@router.post("/admin/api/sync")
async def trigger_sync(request: Request) -> dict:
    _verify_admin_key(request)
    sync_service = getattr(request.app.state, "incremental_sync", None)
    if sync_service is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "增量同步服务未初始化")
    result = await sync_service.sync()
    return {"sync": result}


@router.post("/admin/api/sync/reset")
async def reset_sync(request: Request) -> dict:
    _verify_admin_key(request)
    sync_service = getattr(request.app.state, "incremental_sync", None)
    if sync_service is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "增量同步服务未初始化")
    sync_service.reset_watermark()
    return {"reset": True}


@router.get("/admin/api/writeback/audit")
async def get_audit_log(
    request: Request,
    action: str | None = None,
    asset_id: str | None = None,
    limit: int = 100,
) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "writeback_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "写回管理服务未初始化")
    entries = mgr.read_audit_log(action=action, asset_id=asset_id, limit=limit)
    return {"entries": entries, "count": len(entries)}


@router.post("/admin/api/writeback/rollback")
async def rollback_writeback(request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "writeback_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "写回管理服务未初始化")
    body = await request.json()
    count = body.get("count", 10) if body else 10
    result = await mgr.rollback_recent(count=count)
    return {"rollback": result}


@router.post("/admin/api/writeback/rollback/asset/{asset_id}")
async def rollback_asset(asset_id: str, request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "writeback_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "写回管理服务未初始化")
    result = await mgr.rollback_by_asset(asset_id)
    return {"rollback": result}


@router.post("/admin/api/writeback/descriptions")
async def trigger_description_writeback(request: Request) -> dict:
    """手动触发描述/评分写回。

    Body: {"results": [{"asset_id": "...", "scene": {...}, "final_score": 0.85}]}
    """
    _verify_admin_key(request)
    desc_wb = getattr(request.app.state, "description_writeback", None)
    if desc_wb is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "描述写回服务未初始化")
    body = await request.json()
    results = body.get("results", []) if body else []
    dry_run = body.get("dry_run", False) if body else False
    entries = await desc_wb.writeback_descriptions(results, dry_run=dry_run)
    succeeded = sum(1 for e in entries if e.success)
    return {
        "total": len(entries),
        "succeeded": succeeded,
        "failed": len(entries) - succeeded,
        "entries": [e.model_dump() for e in entries],
    }


@router.get("/admin/api/workflows")
async def list_workflows(request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "workflow_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "工作流管理服务未初始化")
    workflows = await mgr.list_workflows()
    return {"workflows": workflows, "count": len(workflows)}


@router.post("/admin/api/workflows/setup")
async def setup_workflows(request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "workflow_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "工作流管理服务未初始化")
    body = await request.json()
    hook_url = body.get("hook_url", "http://127.0.0.1:8000/api/v1/plugin/hook")
    hook_token = body.get("hook_token", "")
    results = await mgr.setup_default_workflows(hook_url, hook_token)
    return {"workflows": results, "count": len(results)}


@router.delete("/admin/api/workflows/{wf_id}")
async def delete_workflow(wf_id: str, request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "workflow_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "工作流管理服务未初始化")
    success = await mgr.delete_workflow(wf_id)
    return {"deleted": success, "id": wf_id}


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
                ErrorCode.SERVICE_UNAVAILABLE,
                "生产环境必须配置 ADMIN_API_KEY",
            )
        return {"valid": True, "mode": "no-auth"}
    valid = hmac.compare_digest(body.admin_key, expected)
    if not valid:
        raise UnauthorizedError()
    return {"valid": True, "mode": "key-auth"}


@router.get("/admin/api/capabilities")
async def get_capabilities(request: Request) -> dict:
    _verify_admin_key(request)
    cap = getattr(request.app.state, "capabilities", None)
    if cap is None:
        return {"capabilities": None, "message": "能力探测未执行"}
    return {"capabilities": cap.as_dict()}


@router.get("/admin/api/status")
async def system_status(request: Request) -> dict:
    _verify_admin_key(request)
    settings = get_settings()
    features = get_runtime_config().snapshot()
    enabled_count = sum(1 for f in features.values() if f["enabled"])

    cache_backend = type(request.app.state.cache).__name__ if getattr(request.app.state, "cache", None) else "unknown"
    immich_ok = False
    try:
        resp = await request.app.state.immich_client.request_json(
            "GET", "/api/server/ping"
        )
        immich_ok = resp is not None
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


def _mask(value: str) -> str:
    """脱敏：空值返回空串，非空返回前4+后4+星号。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


_SETTING_KEYS = {
    "immich_base_url": "IMMICH_BASE_URL",
    "immich_api_key": "IMMICH_API_KEY",
    "search_api_key": "SEARCH_API_KEY",
    "admin_api_key": "ADMIN_API_KEY",
}

_SENSITIVE_KEYS = {"immich_api_key", "search_api_key", "admin_api_key"}


class SettingsUpdates(BaseModel):
    updates: dict[str, str] = Field(..., min_length=1)


@router.get("/admin/api/settings")
async def get_settings_handler(request: Request) -> dict:
    _verify_admin_key(request)
    settings = get_settings()
    result: dict[str, dict[str, str | bool]] = {}
    for key, env_key in _SETTING_KEYS.items():
        raw = getattr(settings, key)
        result[key] = {
            "env_key": env_key,
            "value": _mask(raw) if key in _SENSITIVE_KEYS else raw,
            "is_set": bool(raw),
            "sensitive": key in _SENSITIVE_KEYS,
        }
    return {"settings": result}


@router.put("/admin/api/settings")
async def update_settings_handler(body: SettingsUpdates, request: Request) -> dict:
    _verify_admin_key(request)
    settings = get_settings()
    if settings.app_env == "production":
        raise AppError(ErrorCode.INVALID_PARAMS, "生产环境禁止通过 API 修改 .env 配置")
    unknown = [k for k in body.updates if k not in _SETTING_KEYS]
    if unknown:
        raise AppError(ErrorCode.INVALID_PARAMS, f"未知的配置项: {','.join(unknown)}")

    env_path = Path(".env")
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    existing: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            existing[k.upper()] = i

    for key, value in body.updates.items():
        env_key = _SETTING_KEYS[key]
        line_text = f"{env_key}={value}"
        if env_key in existing:
            lines[existing[env_key]] = line_text
        else:
            lines.append(line_text)

    tmp_path = env_path.with_suffix(".env.tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        tmp_path.replace(env_path)
    except OSError as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise AppError(ErrorCode.INTERNAL_ERROR, f"配置写入失败: {exc}") from exc

    updated_keys = list(body.updates.keys())
    return {
        "updated": True,
        "keys": updated_keys,
        "message": "配置已写入 .env 文件，重启服务后生效",
    }


def feature_gate(key: str):
    """路由级功能开关依赖：开关关闭时返回 503。"""

    async def _gate() -> None:
        from ...core.config import get_settings as _gs

        settings = _gs()
        settings_default = {
            "ai_enabled": True,
            "feedback_enabled": getattr(settings, "feedback_enabled", True),
            "suggest_enabled": getattr(settings, "suggest_enabled", True),
            "correct_enabled": getattr(settings, "correct_enabled", True),
            "stats_enabled": getattr(settings, "stats_enabled", True),
        }.get(key, True)
        if not runtime_enabled(key, settings_default):
            raise AppError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"功能未启用（{FEATURES_META.get(key, {}).get('name', key)}），可在管理页面开启",
            )

    return _gate