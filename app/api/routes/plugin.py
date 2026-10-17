"""Immich 插件/工作流 hook 端点。

Immich 工作流通过 webhook 方法回调此端点，
触发场景分析 + 标签写回 + 相册写回。

请求格式（Immich webhook 发送）：
    POST /api/v1/plugin/hook
    Headers: x-plugin-token: <token>（可选鉴权）
    Body: {
        "event": "AssetCreate" | "AssetMetadataExtraction" | "AssetTagged",
        "asset": { "id": "...", "originalFileName": "...", ... }
    }

响应格式（Immich 期望）：
    { "changes": {}, "warnings": [], "continue": true }
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from ...core.config import get_settings
from ...core.runtime_config import runtime_enabled
from ..deps import rate_limit_guard

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["plugin"],
    dependencies=[Depends(rate_limit_guard)],
)

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
    """幂等去重 key：event + asset_id + updatedAt（资产更新后允许重新触发）。"""
    raw = f"{event}:{asset.get('id', '')}:{asset.get('updatedAt', '')}"
    return "plugin:hook:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _is_duplicate(request: Request, event: str, asset: dict[str, Any]) -> bool:
    """Redis SETNX 幂等去重；Redis 不可用时放行（降级不阻塞）。"""
    redis_client = getattr(request.app.state, "redis_client", None)
    redis = redis_client.redis if redis_client is not None and redis_client.connected else None
    if redis is None:
        return False
    try:
        first = await redis.set(_dedup_key(event, asset), "1", nx=True, ex=_DEDUP_TTL_SECONDS)
        return first is None or first is False
    except Exception as exc:
        logger.warning("幂等去重检查失败，放行: %s", exc)
        return False


@router.post(
    "/plugin/hook",
    response_model=PluginHookResponse,
    summary="Immich 工作流 hook 回调端点",
    description="Immich 工作流通过 webhook 方法回调此端点，触发场景分析 + 标签写回。",
)
async def plugin_hook(
    payload: PluginHookPayload,
    request: Request,
    x_plugin_token: str | None = Header(default=None),
) -> PluginHookResponse:
    settings = get_settings()
    expected_token = getattr(settings, "plugin_hook_token", "")

    if expected_token and not hmac.compare_digest(x_plugin_token or "", expected_token):
        logger.warning("插件 hook 鉴权失败: token 不匹配")
        return PluginHookResponse(warnings=["鉴权失败"], continue_=False)

    asset_id = payload.asset.get("id")
    if not asset_id:
        logger.warning("插件 hook 收到无 asset.id 的事件: %s", payload.event)
        return PluginHookResponse(warnings=["缺少 asset.id"])

    if await _is_duplicate(request, payload.event, payload.asset):
        logger.info("插件 hook 重复事件，已跳过: event=%s asset_id=%s", payload.event, asset_id)
        return PluginHookResponse(
            changes={"deduplicated": True},
            warnings=["重复事件，已跳过"],
        )

    logger.info(
        "插件 hook 收到事件: event=%s asset_id=%s",
        payload.event, asset_id,
    )

    warnings: list[str] = []
    changes: dict[str, Any] = {}

    scene_writeback = getattr(request.app.state, "scene_writeback", None)
    album_writeback = getattr(request.app.state, "album_writeback", None)
    writeback_enabled = runtime_enabled(
        "writeback_enabled", getattr(settings, "writeback_enabled", False)
    )

    if payload.event == "AssetTagged":
        tags = payload.asset.get("tags", [])
        changes["current_tags"] = tags
        logger.info("AssetTagged 事件: asset=%s tags=%s", asset_id, tags)

    if not writeback_enabled:
        return PluginHookResponse(
            warnings=["写回未启用"] if not warnings else warnings + ["写回未启用"],
            changes=changes,
        )

    if payload.event in ("AssetCreate", "AssetMetadataExtraction"):
        scene_tags = payload.asset.get("scene")
        results_data = [{"asset_id": asset_id, "scene": scene_tags}]

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
        changes["current_tags"] = tags
        logger.info("AssetTagged 事件: asset=%s tags=%s", asset_id, tags)

    return PluginHookResponse(changes=changes, warnings=warnings, continue_=True)
