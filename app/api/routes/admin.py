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
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from ...core.config import get_settings
from ...core.errors import AppError, ErrorCode, UnauthorizedError
from ..deps import rate_limit_guard
from ...core.runtime_config import (
    FEATURES_META,
    get_runtime_config,
    runtime_enabled,
)
from ...services.immich_writeback import DEFAULT_CLEANUP_ALBUM_NAMES

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


async def _parse_json_body(request: Request) -> dict:
    """解析 JSON 请求体；非法 JSON 或非对象时抛 400，而非 500。"""
    try:
        body = await request.json()
    except Exception as exc:
        raise AppError(ErrorCode.INVALID_PARAMS, "请求体必须是合法 JSON") from exc
    if not isinstance(body, dict):
        raise AppError(ErrorCode.INVALID_PARAMS, "请求体必须是 JSON 对象")
    return body


@router.post("/admin/api/writeback/rollback")
async def rollback_writeback(request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "writeback_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "写回管理服务未初始化")
    body = await _parse_json_body(request)
    count = body.get("count", 10)
    if not isinstance(count, int) or count < 1:
        raise AppError(ErrorCode.INVALID_PARAMS, "count 必须是正整数")
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


@router.post("/admin/api/immich/cleanup")
async def immich_cleanup(request: Request) -> dict:
    """清理写回 Immich 的污染数据（AI· 前缀标签/相册 + 冒充入口相册）。

    Body:
        {"dry_run": true,              默认 true，仅扫描返回将删除的清单
         "tag_prefix": "AI·",          标签前缀匹配
         "album_prefix": "AI·",        相册前缀匹配
         "include_album_names": []}    额外精确匹配的相册名
    dry_run=false 才实际删除，逐条审计，单项失败不中断。
    """
    _verify_admin_key(request)
    wb = getattr(request.app.state, "immich_writeback", None)
    if wb is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "写回客户端未初始化")
    body = await _parse_json_body(request)
    dry_run = body.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise AppError(ErrorCode.INVALID_PARAMS, "dry_run 必须是布尔值")
    tag_prefix = body.get("tag_prefix", "AI·")
    if not isinstance(tag_prefix, str) or not tag_prefix.strip():
        raise AppError(ErrorCode.INVALID_PARAMS, "tag_prefix 必须是非空字符串")
    album_prefix = body.get("album_prefix", "AI·")
    if not isinstance(album_prefix, str) or not album_prefix.strip():
        raise AppError(ErrorCode.INVALID_PARAMS, "album_prefix 必须是非空字符串")
    include_names = body.get(
        "include_album_names", list(DEFAULT_CLEANUP_ALBUM_NAMES)
    )
    if not isinstance(include_names, list) or not all(
        isinstance(x, str) for x in include_names
    ):
        raise AppError(ErrorCode.INVALID_PARAMS, "include_album_names 必须是字符串列表")
    return await wb.cleanup(
        tag_prefix=tag_prefix,
        album_prefix=album_prefix,
        album_names=include_names,
        dry_run=dry_run,
    )


@router.post("/admin/api/writeback/descriptions")
async def trigger_description_writeback(request: Request) -> dict:
    """手动触发描述/评分写回。

    Body: {"results": [{"asset_id": "...", "scene": {...}, "final_score": 0.85}]}
    """
    _verify_admin_key(request)
    desc_wb = getattr(request.app.state, "description_writeback", None)
    if desc_wb is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "描述写回服务未初始化")
    body = await _parse_json_body(request)
    results = body.get("results", [])
    if not isinstance(results, list):
        raise AppError(ErrorCode.INVALID_PARAMS, "results 必须是列表")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise AppError(ErrorCode.INVALID_PARAMS, "dry_run 必须是布尔值")
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
    settings = get_settings()
    callback_base = settings.plugin_callback_base_url or "http://host.docker.internal:8000"
    expected_prefix = callback_base.rstrip("/")
    workflows = mgr.annotate_workflow_reachability(workflows, expected_prefix)
    return {"workflows": workflows, "count": len(workflows), "expected_callback_base": expected_prefix}


@router.post("/admin/api/workflows/setup")
async def setup_workflows(request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "workflow_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "工作流管理服务未初始化")
    body = await _parse_json_body(request)
    hook_url = body.get("hook_url", "")
    hook_token = body.get("hook_token", "")
    if not hook_url or not isinstance(hook_url, str) or any(c.isspace() for c in hook_url):
        raise AppError(
            ErrorCode.INVALID_PARAMS,
            "必须显式提供 hook_url（从 Immich 容器可达的本服务地址，"
            "如 http://host.docker.internal:8000/api/v1/plugin/hook）",
        )
    if not hook_url.startswith(("http://", "https://")):
        raise AppError(ErrorCode.INVALID_PARAMS, "hook_url 必须是 http(s) URL")
    results = await mgr.setup_default_workflows(hook_url, hook_token)
    return {"workflows": results, "count": len(results)}


@router.post("/admin/api/immich/workflows/sync")
async def sync_workflows(request: Request) -> dict:
    """幂等同步默认工作流到 Immich，返回 diff。"""
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "workflow_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "工作流管理服务未初始化")
    settings = get_settings()
    from ...services.callback_resolver import (
        resolve_callback_base_url,
        resolve_hook_url,
    )
    callback_base = await resolve_callback_base_url(settings)
    hook_url = resolve_hook_url(callback_base)
    hook_token = settings.plugin_hook_token.get_secret_value() if settings.plugin_hook_token else ""
    diff = await mgr.sync_default_workflows(hook_url, hook_token)
    stale_deleted = await mgr.delete_stale_workflows()
    return {
        "diff": diff,
        "stale_deleted": stale_deleted,
        "hook_url": hook_url,
        "callback_base_url": callback_base,
    }


@router.delete("/admin/api/workflows/{wf_id}")
async def delete_workflow(wf_id: str, request: Request) -> dict:
    _verify_admin_key(request)
    mgr = getattr(request.app.state, "workflow_manager", None)
    if mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "工作流管理服务未初始化")
    success = await mgr.delete_workflow(wf_id)
    return {"deleted": success, "id": wf_id}


@router.post("/admin/api/backfill/scene")
async def trigger_backfill(request: Request) -> dict:
    """触发历史资产回填（场景分析 + 标签写回）。

    Body: {"limit": 0, "since": "", "dry_run": false, "skip_tagged": true}
    """
    _verify_admin_key(request)
    svc = getattr(request.app.state, "backfill_service", None)
    if svc is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "回填服务未初始化")
    body = await request.json()
    limit = body.get("limit", 0) if body else 0
    since = body.get("since", "") if body else ""
    dry_run = body.get("dry_run", False) if body else False
    skip_tagged = body.get("skip_tagged", True) if body else True
    result = await svc.run_backfill(
        limit=limit, since=since, dry_run=dry_run, skip_tagged=skip_tagged
    )
    return {"backfill": result}


@router.get("/admin/api/backfill/status")
async def backfill_status(request: Request) -> dict:
    """查询回填任务进度（内存优先，任务不在内存时回退 Redis 快照）。"""
    _verify_admin_key(request)
    svc = getattr(request.app.state, "backfill_service", None)
    if svc is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "回填服务未初始化")
    return {"status": await svc.get_status()}


@router.get("/admin/api/search/translate")
async def debug_search_translate(request: Request) -> dict:
    """人工核对：中文查询 → 英文 CLIP 检索短语（本地词表路径，不调外部 API）。

    Query: ?q=海边日落
    """
    _verify_admin_key(request)
    from ...scene.query_translate import translate_query_detailed

    q = request.query_params.get("q", "")
    return translate_query_detailed(q)


@router.get("/admin/api/ml/health")
async def ml_health(request: Request) -> dict:
    """ML 服务健康检查。"""
    _verify_admin_key(request)
    inferrer = getattr(request.app.state, "ml_inferrer", None)
    model_mgr = getattr(request.app.state, "model_manager", None)
    result: dict[str, Any] = {}
    if inferrer is not None:
        result["inferrer"] = {"name": inferrer.name, "healthy": await inferrer.probe()}
    if model_mgr is not None:
        result["models"] = await model_mgr.health_check()
    return result


@router.get("/admin/api/ml/models")
async def list_models(request: Request) -> dict:
    """列出所有已注册 ML 模型。"""
    _verify_admin_key(request)
    model_mgr = getattr(request.app.state, "model_manager", None)
    if model_mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "模型管理服务未初始化")
    models = model_mgr.list_models()
    return {"models": [m.__dict__ for m in models], "count": len(models)}


@router.post("/admin/api/ml/models/{name}/load")
async def load_model(name: str, request: Request) -> dict:
    """加载指定模型。"""
    _verify_admin_key(request)
    model_mgr = getattr(request.app.state, "model_manager", None)
    if model_mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "模型管理服务未初始化")
    try:
        await model_mgr.load(name)
        return {"loaded": True, "name": name}
    except Exception as exc:
        return {"loaded": False, "name": name, "error": str(exc)}


@router.post("/admin/api/ml/models/{name}/unload")
async def unload_model(name: str, request: Request) -> dict:
    """卸载指定模型。"""
    _verify_admin_key(request)
    model_mgr = getattr(request.app.state, "model_manager", None)
    if model_mgr is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "模型管理服务未初始化")
    success = await model_mgr.unload(name)
    return {"unloaded": success, "name": name}


@router.get("/admin/api/vector/stats")
async def vector_stats(request: Request) -> dict:
    """向量索引统计。"""
    _verify_admin_key(request)
    vi = getattr(request.app.state, "vector_index", None)
    if vi is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "向量索引服务未初始化")
    return await vi.stats()


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


@router.post("/admin/api/verify", dependencies=[Depends(rate_limit_guard)])
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
        "writeback": {
            "enabled": settings.writeback_enabled,
            "source": ".env WRITEBACK_ENABLED（单一来源，无运行时覆盖）",
            "whitelist": "source=analyzed 且置信度 >= scene_min_confidence",
            "min_confidence": settings.scene_min_confidence,
            "dedup_ttl_seconds": settings.writeback_dedup_ttl_seconds,
        },
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

    @field_validator("updates")
    @classmethod
    def _no_newline_injection(cls, value: dict[str, str]) -> dict[str, str]:
        """拒绝包含换行符的配置值，防止 .env 注入任意配置行。"""
        for key, val in value.items():
            if "\n" in val or "\r" in val:
                raise ValueError(f"配置值不能包含换行符: {key}")
        return value


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
    if not settings.admin_api_key:
        # 硬闸：未配置 ADMIN_API_KEY 时任何环境都禁止写 .env。
        # 否则 development 默认放行 + 可改 immich_base_url 组成凭据外送链
        # （服务重启后 IMMICH_API_KEY 会被明文发往攻击者地址）。
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "写 .env 前必须配置 ADMIN_API_KEY（防止未授权修改上游地址导致 API Key 外送）",
        )
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
