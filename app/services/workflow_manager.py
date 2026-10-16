"""工作流管理服务：创建/管理 Immich 原生工作流。

Immich v3.3.1 工作流 API：
    POST /api/workflows          — 创建工作流
    PUT  /api/workflows/{id}     — 更新工作流（含 steps）
    GET  /api/workflows          — 列出所有工作流
    DELETE /api/workflows/{id}   — 删除工作流

工作流 step 格式：
    {
        "method": "immich-plugin-core#<methodName>",
        "config": { ... },       # 方法参数
        "enabled": true
    }

可用方法（hostFunctions=true 的写回类）：
    assetAddTags      — config: {"tags": ["AI/自然"]}
    assetAddToAlbums  — config: {"albumIds": [], "albumName": "名称"}
    webhook           — config: {"url": "...", "method": "POST", "headerName": "...", "headerValue": "..."}

触发器：
    AssetCreate              — 新资产上传
    AssetMetadataExtraction  — 元数据提取完成
    AssetTagged              — 标签添加完成
"""
from __future__ import annotations

import logging
from typing import Any

from ..core.config import Settings
from .immich import ImmichClient

logger = logging.getLogger(__name__)

_PLUGIN_PREFIX = "immich-plugin-core#"


class WorkflowManager:
    def __init__(self, immich: ImmichClient, settings: Settings) -> None:
        self._immich = immich
        self._settings = settings

    async def list_workflows(self) -> list[dict[str, Any]]:
        try:
            data = await self._immich.request_json("GET", "/api/workflows")
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("列出工作流失败: %s", exc)
            return []

    async def create_workflow(
        self,
        name: str,
        trigger: str,
        description: str = "",
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "name": name,
            "trigger": trigger,
            "description": description,
            "enabled": True,
            "steps": [],
        }
        try:
            result = await self._immich.request_json(
                "POST", "/api/workflows", json=payload
            )
            wf_id = result.get("id") if isinstance(result, dict) else None
            if wf_id and steps:
                updated = await self.update_workflow(wf_id, name, trigger, description, steps)
                return updated
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.error("创建工作流失败 name=%s: %s", name, exc)
            return None

    async def update_workflow(
        self,
        wf_id: str,
        name: str,
        trigger: str,
        description: str = "",
        steps: list[dict[str, Any]] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any] | None:
        payload = {
            "name": name,
            "trigger": trigger,
            "description": description,
            "enabled": enabled,
            "steps": steps or [],
        }
        try:
            result = await self._immich.request_json(
                "PUT", f"/api/workflows/{wf_id}", json=payload
            )
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.error("更新工作流失败 id=%s: %s", wf_id, exc)
            return None

    async def delete_workflow(self, wf_id: str) -> bool:
        try:
            await self._immich.request_json("DELETE", f"/api/workflows/{wf_id}")
            return True
        except Exception as exc:
            logger.warning("删除工作流失败 id=%s: %s", wf_id, exc)
            return False

    async def get_workflow(self, wf_id: str) -> dict[str, Any] | None:
        try:
            result = await self._immich.request_json("GET", f"/api/workflows/{wf_id}")
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.warning("获取工作流失败 id=%s: %s", wf_id, exc)
            return None

    @staticmethod
    def make_webhook_step(
        url: str,
        method: str = "POST",
        header_name: str = "",
        header_value: str = "",
        step_name: str = "webhook",
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"url": url, "method": method}
        if header_name and header_value:
            config["headerName"] = header_name
            config["headerValue"] = header_value
        return {
            "method": f"{_PLUGIN_PREFIX}webhook",
            "config": config,
            "enabled": True,
        }

    @staticmethod
    def make_add_tags_step(tags: list[str]) -> dict[str, Any]:
        return {
            "method": f"{_PLUGIN_PREFIX}assetAddTags",
            "config": {"tags": tags},
            "enabled": True,
        }

    @staticmethod
    def make_add_to_album_step(album_name: str, album_ids: list[str] | None = None) -> dict[str, Any]:
        config: dict[str, Any] = {"albumIds": album_ids or []}
        if album_name:
            config["albumName"] = album_name
        return {
            "method": f"{_PLUGIN_PREFIX}assetAddToAlbums",
            "config": config,
            "enabled": True,
        }

    async def setup_default_workflows(
        self, hook_url: str, hook_token: str = ""
    ) -> list[dict[str, Any]]:
        """创建三个默认工作流模板。"""
        header_name = "x-plugin-token" if hook_token else ""
        results: list[dict[str, Any]] = []

        wf1 = await self.create_workflow(
            name="yunying-on2-on-upload",
            trigger="AssetCreate",
            description="上传即分析：新资产上传后回调云影图鉴进行场景分析+标签写回",
            steps=[self.make_webhook_step(hook_url, "POST", header_name, hook_token)],
        )
        if wf1:
            results.append(wf1)

        wf2 = await self.create_workflow(
            name="yunying2-on-metadata",
            trigger="AssetMetadataExtraction",
            description="元数据补全：EXIF 提取完成后回调云影图鉴补全场景描述",
            steps=[self.make_webhook_step(hook_url, "POST", header_name, hook_token)],
        )
        if wf2:
            results.append(wf2)

        wf3 = await self.create_workflow(
            name="yunying2-on-tagged",
            trigger="AssetTagged",
            description="打标后归档：标签添加完成后回调云影图鉴检查是否需要自动归档",
            steps=[self.make_webhook_step(hook_url, "POST", header_name, hook_token)],
        )
        if wf3:
            results.append(wf3)

        logger.info("默认工作流创建完成: %d 个", len(results))
        return results