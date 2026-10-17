"""Immich 插件/工作流 hook 端点。

Immich 工作流通过 webhook 方法回调此端点，
触发场景分析 + 标签写回 + 相册写回。

请求格式（Immich webhook 发送）：
    POST /api/v1/plugin/hook
    Headers: x-plugin-token: <token>（鉴权，非空时强制校验）
    Body: {
        "event": "AssetCreate" | "AssetMetadataExtraction" | "AssetTagged",
        "asset": { "id": "...", "originalFileName": "...", ... }
    }

响应格式（Immich 期望）：
    { "changes": {}, "warnings": [], "continue": true }

加固措施：
- 鉴权：PLUGIN_HOOK_TOKEN 非空时强制校验，失败返回 401
- 幂等：以 (event, asset.id, asset.updatedAt) 做 Redis SETNX 去重，TTL 1h
- 限流：复用 TokenBucketRateLimiter，key 前缀 plugin-hook
- 观测：结构化日志记录 request_id/event/assetId/耗时/写回结果
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from ...core.config import get_settings
from ...core.runtime_config import runtime_enabled
from ..deps import rate_limit_guard

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plugin"], dependencies=[Depends(rate_limit_guard)])

_DEDUP_TTL_SECONDS = 3600


class PluginHookPayload(BaseModel):
    event: str = Field(description="AssetCreate | AssetMetadataExtraction | AssetTagged")
    asset: dict[str, Any] = Field(default_factory=dict)


class PluginHookResponse(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    continue_: bool = Field(default=True, alias="continue")

    model_config = {"populate_by_name": True}


def _dedup_key(event: str, asset: dict[str, Any]) -> str:
    """生成幂等去重 key：(event, asset.id, asset.updatedAt)。"""
    asset_id = asset.get("id", "")
    updated_at = asset.get("updatedAt", asset.get("fileCreatedAt", ""))
    raw = f"{event}:{asset_id}:{updated_at}"
    return f"plugin-hook:dedup:{hashlib.sha256(raw.encode()).hexdigest()}"


async def _check_dedup(request: Request, dedup_key: str) -> bool:
    """Redis SETNX 去重。返回 True 表示首次（应处理），False 表示重复（应跳过）。"""
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None or not redis_client.connected:
        return True
    try:
        redis = redis_client.redis
        result = await redis.set(dedup_key, "1", ex=_DEDUP_TTL_SECONDS, nx=True)
        return result is not None and result is not False
    except Exception as exc:
        logger.warning("幂等去重检查失败，放行: %s", exc)
        return True


@router.post(
    "/plugin/hook",
    response_model=PluginHookResponse,
    summary="Immich 工作流 hook 回调端点",
    description="Immich 工作流通过 webhook 方法回调此端点，触发场景分析 + 标签写回。",
)
async def plugin_hook(
    payload: PluginHookPayload,
    request: Request,
    response: Response,
    x_plugin_token: str | None = Header(default=None),
) -> PluginHookResponse:
    settings = get_settings()
    request_id = getattr(request.state, "request_id", "")
    t0 = time.monotonic()

    expected_token = ""
    if settings.plugin_hook_token:
        expected_token = settings.plugin_hook_token.get_secret_value()

    if expected_token:
        import hmac
        if not x_plugin_token or not hmac.compare_digest(x_plugin_token, expected_token):
            logger.warning(
                "插件 hook 鉴权失败 request_id=%s event=%s",
                request_id, payload.event,
            )
            response.status_code = 401
            return PluginHookResponse(warnings=["鉴权失败"], continue_=False)

    asset_id = payload.asset.get("id")
    if not asset_id:
        logger.warning(
            "插件 hook 收到无 asset.id request_id=%s event=%s",
            request_id, payload.event,
        )
        return PluginHookResponse(warnings=["缺少 asset.id"])

    dedup_key = _dedup_key(payload.event, payload.asset)
    is_first = await _check_dedup(request, dedup_key)
    if not is_first:
        logger.info(
            "插件 hook 幂等跳过 request_id=%s event=%s asset_id=%s",
            request_id, payload.event, asset_id,
        )
        return PluginHookResponse(
            changes={"deduplicated": True},
            warnings=["重复请求已跳过"],
            continue_=True,
        )

    logger.info(
        "插件 hook 收到事件 request_id=%s event=%s asset_id=%s",
        request_id, payload.event, asset_id,
    )

    warnings: list[str] = []
    changes: dict[str, Any] = {}

    if payload.event == "AssetTagged":
        changes["current_tags"] = payload.asset.get("tags", [])

    scene_writeback = getattr(request.app.state, "scene_writeback", None)
    album_writeback = getattr(request.app.state, "album_writeback", None)
    desc_writeback = getattr(request.app.state, "description_writeback", None)
    writeback_enabled = runtime_enabled(
        "writeback_enabled", getattr(settings, "writeback_enabled", False)
    )

    if not writeback_enabled:
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        logger.info(
            "插件 hook 完成 request_id=%s event=%s asset_id=%s elapsed_ms=%s writeback=disabled",
            request_id, payload.event, asset_id, elapsed,
        )
        return PluginHookResponse(
            warnings=["写回未启用"],
            changes=changes,
        )

    results_data = [{"asset_id": asset_id, "scene": payload.asset.get("scene")}]

    if payload.event in ("AssetCreate", "AssetMetadataExtraction"):
        if scene_writeback is not None:
            try:
                entries = await scene_writeback.writeback_scene_tags(results_data)
                succeeded = sum(1 for e in entries if e.success)
                changes["tags_written"] = succeeded
                if succeeded < len(entries):
                    warnings.append(f"部分标签写回失败: {succeeded}/{len(entries)}")
            except Exception as exc:
                logger.warning("场景标签写回失败: %s", exc)
                warnings.append(f"标签写回异常: {exc}")

        if desc_writeback is not None:
            try:
                desc_data = [
                    {
                        "asset_id": asset_id,
                        "scene": payload.asset.get("scene"),
                        "final_score": payload.asset.get("final_score", 0.0),
                        "similarity_score": payload.asset.get("similarity_score", 0.0),
                    }
                ]
                await desc_writeback.writeback_descriptions(desc_data)
                changes["description_written"] = True
            except Exception as exc:
                logger.warning("描述写回失败: %s", exc)
                warnings.append(f"描述写回异常: {exc}")

        if album_writeback is not None:
            try:
                entries = await album_writeback.writeback_albums(results_data)
                succeeded = sum(1 for e in entries if e.success)
                changes["albums_updated"] = succeeded
            except Exception as exc:
                logger.warning("相册写回失败: %s", exc)
                warnings.append(f"相册写回异常: {exc}")

    elif payload.event == "AssetTagged":
        tags = payload.asset.get("tags", [])

        logger.info("AssetTagged 事件: asset=%s tags=%s", asset_id, tags)

        if album_writeback is not None:
            try:
                entries = await album_writeback.writeback_albums(results_data)
                succeeded = sum(1 for e in entries if e.success)
                changes["albums_updated"] = succeeded
            except Exception as exc:
                logger.warning("相册归档失败: %s", exc)
                warnings.append(f"相册归档异常: {exc}")

    elapsed = round((time.monotonic() - t0) * 1000, 1)
    logger.info(
        "插件 hook 完成 request_id=%s event=%s asset_id=%s elapsed_ms=%s changes=%s",
        request_id, payload.event, asset_id, elapsed, changes,
    )

    return PluginHookResponse(changes=changes, warnings=warnings, continue_=True)
