"""P22: ML 场景推理服务 — 可插拔多后端场景推理。

支持三种后端：
- local: 本地词典推理（默认，零依赖）
- http:  远程 HTTP 推理端点（CLIP / 华为云等）
- batch: 批量推理封装（并发调度 + 结果合并）

设计要点：
- 继承 SceneInferrer 抽象基类，与现有 analyzer 无缝集成
- 批量推理：asyncio.gather 并发 + 每项超时 + 失败跳过
- 健康检查：probe() 方法返回后端可用性
- 可配置：通过 SCENE_INFERENCE_ENDPOINT 切换后端
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..core.config import Settings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_BATCH_CONCURRENCY = 8


class MLSceneInferrer(ABC):
    """ML 场景推理抽象基类。"""

    name: str = "abstract"

    @abstractmethod
    async def infer(self, asset: dict[str, Any]) -> list[str]:
        """单图推理：返回场景 key 列表。"""

    async def infer_batch(
        self,
        assets: list[dict[str, Any]],
        concurrency: int = _DEFAULT_BATCH_CONCURRENCY,
    ) -> list[list[str]]:
        """批量推理：并发调度，失败项返回空列表。"""
        sem = asyncio.Semaphore(concurrency)
        results: list[list[str]] = []

        async def _infer_one(asset: dict[str, Any]) -> list[str]:
            async with sem:
                try:
                    return await self.infer(asset)
                except Exception as exc:
                    asset_id = asset.get("id") if isinstance(asset, dict) else None
                    logger.warning("批量推理单项失败 asset=%s: %s", asset_id, exc)
                    return []

        results = await asyncio.gather(*[_infer_one(a) for a in assets])
        return list(results)

    async def probe(self) -> bool:
        """健康检查：返回后端是否可用。"""
        return True


class LocalMLInferrer(MLSceneInferrer):
    """本地词典推理（零依赖，基于 Immich smartInfo/tags 映射）。"""

    name = "local"

    _SMART_FIELD_MAP = {
        "nature": {"nature", "landscape", "outdoor", "beach", "mountain", "forest"},
        "city": {"city", "urban", "street", "building", "architecture"},
        "people": {"people", "person", "portrait", "face", "human"},
        "life": {"food", "pet", "home", "interior", "lifestyle"},
        "event": {"event", "party", "wedding", "concert", "festival"},
        "animal": {"animal", "dog", "cat", "bird", "wildlife"},
        "architecture": {"architecture", "temple", "church", "bridge"},
        "night": {"night", "dark", "starry", "moon", "neon"},
        "vehicle": {"car", "vehicle", "bike", "boat", "plane"},
        "art": {"art", "painting", "sculpture", "design"},
    }

    async def infer(self, asset: dict[str, Any]) -> list[str]:
        tags = self._extract_tags(asset)
        if not tags:
            return []
        matched: set[str] = set()
        tags_lower = {t.lower() for t in tags}
        for scene_key, keywords in self._SMART_FIELD_MAP.items():
            if keywords & tags_lower:
                matched.add(scene_key)
        return sorted(matched)

    def _extract_tags(self, asset: dict[str, Any]) -> list[str]:
        tags: list[str] = []
        smart_info = asset.get("smartInfo") or {}
        if isinstance(smart_info, dict):
            for v in (smart_info.get("tags") or []):
                if isinstance(v, str):
                    tags.append(v)
                elif isinstance(v, dict):
                    val = v.get("value") or v.get("name")
                    if val:
                        tags.append(str(val))
        asset_tags = asset.get("tags") or []
        for t in asset_tags:
            if isinstance(t, dict):
                val = t.get("value") or t.get("name")
                if val:
                    tags.append(str(val))
            elif isinstance(t, str):
                tags.append(t)
        return tags


class HttpMLInferrer(MLSceneInferrer):
    """远程 HTTP 推理端点（CLIP / 华为云等）。"""

    name = "http"

    def __init__(
        self,
        endpoint: str,
        timeout: float = _DEFAULT_TIMEOUT,
        api_key: str = "",
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._api_key = api_key

    async def infer(self, asset: dict[str, Any]) -> list[str]:
        async with httpx.AsyncClient(
            timeout=self._timeout, trust_env=False
        ) as client:
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            resp = await client.post(
                f"{self._endpoint}/infer",
                json={"asset": asset},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            scenes = data.get("scenes") or []
            return [s for s in scenes if isinstance(s, str)]

    async def probe(self) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=5.0, trust_env=False
            ) as client:
                resp = await client.get(f"{self._endpoint}/health")
                return resp.status_code == 200
        except Exception:
            return False


def create_ml_inferrer(settings: Settings) -> MLSceneInferrer:
    """工厂方法：根据配置选择推理后端。"""
    endpoint = getattr(settings, "scene_inference_endpoint", "") or ""
    if endpoint:
        return HttpMLInferrer(endpoint)
    return LocalMLInferrer()
