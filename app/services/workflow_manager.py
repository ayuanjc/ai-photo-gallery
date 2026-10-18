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
_OWNED_PREFIX = "yunying"

_DEFAULT_WORKFLOWS: list[dict[str, str]] = [
    {
        "name": "yunying-on-upload",
        "trigger": "AssetCreate",
        "description": "上传即分析：新资产上传后回调云影图鉴进行场景分析+标签写回",
    },
    {
        "name": "yunying-on-metadata",
        "trigger": "AssetMetadataExtraction",
        "description": "元数据补全：EXIF 提取完成后回调云影图鉴补全场景描述",
    },
    {
        "name": "yunying-on-tagged",
        "trigger": "AssetTagged",
        "description": "打标后归档：标签添加完成后回调云影图鉴检查是否需要自动归档",
    },
]


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
        logging_enabled: bool = True,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "name": name,
            "trigger": trigger,
            "description": description,
            "enabled": True,
            "logging": logging_enabled,
            "steps": [],
        }
        try:
            result = await self._immich.request_json(
                "POST", "/api/workflows", json=payload
            )
            wf_id = result.get("id") if isinstance(result, dict) else None
            if wf_id and steps:
                updated = await self.update_workflow(
                    wf_id, name, trigger, description, steps,
                    logging_enabled=logging_enabled,
                )
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
        logging_enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        """更新工作流。logging_enabled=None 时不修改 logging 字段。"""
        payload: dict[str, Any] = {
            "name": name,
            "trigger": trigger,
            "description": description,
            "enabled": enabled,
            "steps": steps or [],
        }
        if logging_enabled is not None:
            payload["logging"] = logging_enabled
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

        for wf_def in _DEFAULT_WORKFLOWS:
            wf = await self.create_workflow(
                name=wf_def["name"],
                trigger=wf_def["trigger"],
                description=wf_def["description"],
                steps=[self.make_webhook_step(hook_url, "POST", header_name, hook_token)],
            )
            if wf:
                results.append(wf)

        logger.info("默认工作流创建完成: %d 个", len(results))
        return results

    async def sync_default_workflows(
        self,
        hook_url: str,
        hook_token: str = "",
    ) -> dict[str, Any]:
        """幂等 upsert 三条默认工作流，返回 diff。

        返回: {"created": [...], "updated": [...], "unchanged": [...], "errors": [...]}
        """
        header_name = "x-plugin-token" if hook_token else ""
        logging_enabled = getattr(self._settings, "immich_workflow_logging", True)

        existing = await self.list_workflows()
        existing_by_name: dict[str, dict[str, Any]] = {}
        for wf in existing:
            name = wf.get("name", "")
            if name:
                existing_by_name[name] = wf

        result: dict[str, Any] = {
            "created": [],
            "updated": [],
            "unchanged": [],
            "errors": [],
        }

        for wf_def in _DEFAULT_WORKFLOWS:
            name = wf_def["name"]
            trigger = wf_def["trigger"]
            description = wf_def["description"]
            desired_steps = [
                self.make_webhook_step(hook_url, "POST", header_name, hook_token)
            ]

            if name in existing_by_name:
                wf = existing_by_name[name]
                wf_id = wf.get("id", "")
                needs_update = self._workflow_needs_update(
                    wf, trigger, description, desired_steps, logging_enabled
                )
                if needs_update:
                    updated = await self.update_workflow(
                        wf_id, name, trigger, description,
                        steps=desired_steps, enabled=True,
                        logging_enabled=logging_enabled,
                    )
                    if updated:
                        result["updated"].append({"name": name, "id": wf_id})
                    else:
                        result["errors"].append({"name": name, "reason": "更新失败"})
                else:
                    result["unchanged"].append({"name": name, "id": wf_id})
            else:
                created = await self.create_workflow(
                    name=name, trigger=trigger, description=description,
                    steps=desired_steps, logging_enabled=logging_enabled,
                )
                if created:
                    result["created"].append({"name": name, "id": created.get("id")})
                else:
                    result["errors"].append({"name": name, "reason": "创建失败"})

        logger.info(
            "工作流同步完成: created=%d updated=%d unchanged=%d errors=%d",
            len(result["created"]), len(result["updated"]),
            len(result["unchanged"]), len(result["errors"]),
        )
        return result

    async def delete_stale_workflows(self) -> list[str]:
        """删除以 yunying 前缀开头但不属于默认工作流的陈旧工作流。

        仅删除我们自己创建的（name 以 _OWNED_PREFIX 开头），
        且不在当前默认工作流名称集合中的。
        """
        default_names = {wf["name"] for wf in _DEFAULT_WORKFLOWS}
        existing = await self.list_workflows()
        deleted: list[str] = []

        for wf in existing:
            name = wf.get("name", "")
            wf_id = wf.get("id", "")
            if not (
                name.startswith(_OWNED_PREFIX)
                and name not in default_names
                and wf_id
            ):
                continue
            if await self.delete_workflow(wf_id):
                deleted.append(name)
                logger.info("删除陈旧工作流: %s (%s)", name, wf_id)

        return deleted

    @staticmethod
    def _workflow_needs_update(
        wf: dict[str, Any],
        desired_trigger: str,
        desired_description: str,
        desired_steps: list[dict[str, Any]],
        desired_logging: bool,
    ) -> bool:
        """比较现有工作流与期望状态，判断是否需要更新。"""
        if wf.get("trigger") != desired_trigger:
            return True
        if wf.get("description") != desired_description:
            return True
        if not wf.get("enabled", False):
            return True
        if wf.get("logging", False) != desired_logging:
            return True

        existing_steps = wf.get("steps", [])
        if len(existing_steps) != len(desired_steps):
            return True

        for existing_step, desired_step in zip(existing_steps, desired_steps, strict=False):
            if existing_step.get("method") != desired_step.get("method"):
                return True
            existing_config = existing_step.get("config", {})
            desired_config = desired_step.get("config", {})
            if existing_config.get("url") != desired_config.get("url"):
                return True
            if existing_config.get("method") != desired_config.get("method"):
                return True
            if existing_config.get("headerName") != desired_config.get("headerName"):
                return True
            if existing_config.get("headerValue") != desired_config.get("headerValue"):
                return True
            if not existing_step.get("enabled", False):
                return True

        return False

    @staticmethod
    def annotate_workflow_reachability(
        workflows: list[dict[str, Any]],
        expected_url_prefix: str = "",
    ) -> list[dict[str, Any]]:
        """为每条工作流标注 step url 是否可达。

        参数:
            workflows: 工作流列表。
            expected_url_prefix: 期望的回调基地址前缀（用于判断是否配置正确）。
        """
        for wf in workflows:
            steps = wf.get("steps", [])
            urls: list[str] = []
            reachable = True
            for step in steps:
                config = step.get("config", {})
                url = config.get("url", "")
                if url:
                    urls.append(url)
                    if expected_url_prefix and not url.startswith(expected_url_prefix):
                        reachable = False
            wf["hook_urls"] = urls
            wf["url_reachable"] = reachable
        return workflows
