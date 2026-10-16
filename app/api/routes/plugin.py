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

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from ...core.config import get_settings
from ...core.runtime_config import runtime_enabled
from ..deps import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plugin"])


class PluginHookPayload(BaseModel):
    event: str = Field(description="AssetCreate | AssetMetadataExtraction | AssetTagged")
    asset: dict[str, Any] = Field(default_factory=dict)


class PluginHookResponse(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    continue_: bool = Field(default=True, alias="continue")

    model_config = {"populate_by_name": True}


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

    if expected_token and x_plugin_token != expected_token:
        logger.warning("插件 hook 鉴权失败: token 不匹配")
        return PluginHookResponse(warnings=["鉴权失败"], continue_=False)

    asset_id = payload.asset.get("id")
    if not asset_id:
        logger.warning("插件 hook 收到无 asset.id 的事件: %s", payload.event)
        return PluginHookResponse(warnings=["缺少 asset.id"])

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