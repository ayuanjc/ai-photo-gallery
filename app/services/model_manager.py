"""P24: 模型管理服务 — ML 模型生命周期管理。

功能：
- register(name, loader): 注册模型加载器
- load(name): 加载模型到内存
- unload(name): 卸载模型
- health_check(): 全量健康检查
- get_model(name): 获取已加载模型

设计要点：
- 懒加载：首次 get_model 时自动 load
- 引用计数：多次 load 需对应多次 unload
- 超时保护：加载超时自动失败
- 状态追踪：loaded / loading / error / unloaded
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

ModelLoader = Callable[[], Awaitable[Any]]


class ModelStatus(str, Enum):
    REGISTERED = "registered"
    LOADING = "loading"
    LOADED = "loaded"
    ERROR = "error"
    UNLOADED = "unloaded"


@dataclass
class ModelInfo:
    name: str
    status: ModelStatus = ModelStatus.REGISTERED
    ref_count: int = 0
    error: str | None = None
    loaded_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelManager:
    """ML 模型生命周期管理器。"""

    def __init__(self, load_timeout: float = 30.0) -> None:
        self._loaders: dict[str, ModelLoader] = {}
        self._models: dict[str, Any] = {}
        self._infos: dict[str, ModelInfo] = {}
        self._lock = asyncio.Lock()
        self._load_timeout = load_timeout

    def register(
        self,
        name: str,
        loader: ModelLoader,
        metadata: dict[str, Any] | None = None,
    ) -> ModelInfo:
        """注册模型加载器。"""
        info = ModelInfo(name=name, metadata=metadata or {})
        self._loaders[name] = loader
        self._infos[name] = info
        logger.info("模型注册: %s", name)
        return info

    def unregister(self, name: str) -> bool:
        """注销模型（需先 unload）。"""
        info = self._infos.get(name)
        if info is None:
            return False
        if info.status == ModelStatus.LOADED:
            return False
        self._loaders.pop(name, None)
        self._infos.pop(name, None)
        return True

    async def load(self, name: str) -> Any:
        """加载模型，返回模型实例。"""
        async with self._lock:
            info = self._infos.get(name)
            if info is None:
                raise KeyError(f"模型未注册: {name}")

            if info.status == ModelStatus.LOADED:
                info.ref_count += 1
                return self._models.get(name)

            if info.status == ModelStatus.LOADING:
                raise RuntimeError(f"模型正在加载中: {name}")

            loader = self._loaders.get(name)
            if loader is None:
                raise KeyError(f"加载器不存在: {name}")

            info.status = ModelStatus.LOADING
            info.error = None

        try:
            model = await asyncio.wait_for(loader(), timeout=self._load_timeout)
            async with self._lock:
                info = self._infos[name]
                info.status = ModelStatus.LOADED
                info.ref_count = 1
                info.loaded_at = time.time()
                self._models[name] = model
            logger.info("模型加载成功: %s", name)
            return model
        except Exception as exc:
            async with self._lock:
                info = self._infos[name]
                info.status = ModelStatus.ERROR
                info.error = str(exc)
            logger.error("模型加载失败: %s — %s", name, exc)
            raise

    async def unload(self, name: str) -> bool:
        """卸载模型（引用计数归零时实际卸载）。"""
        async with self._lock:
            info = self._infos.get(name)
            if info is None or info.status != ModelStatus.LOADED:
                return False
            info.ref_count -= 1
            if info.ref_count <= 0:
                self._models.pop(name, None)
                info.status = ModelStatus.UNLOADED
                info.ref_count = 0
                logger.info("模型卸载: %s", name)
            return True

    async def get_model(self, name: str) -> Any:
        """获取已加载模型（懒加载）。"""
        info = self._infos.get(name)
        if info is None:
            raise KeyError(f"模型未注册: {name}")
        if info.status == ModelStatus.LOADED:
            return self._models.get(name)
        return await self.load(name)

    async def health_check(self) -> dict[str, dict[str, Any]]:
        """全量健康检查。"""
        result: dict[str, dict[str, Any]] = {}
        for name, info in self._infos.items():
            result[name] = {
                "status": info.status.value,
                "ref_count": info.ref_count,
                "error": info.error,
                "loaded_at": info.loaded_at,
            }
        return result

    def list_models(self) -> list[ModelInfo]:
        """列出所有已注册模型。"""
        return list(self._infos.values())

    def is_loaded(self, name: str) -> bool:
        """检查模型是否已加载。"""
        info = self._infos.get(name)
        return info is not None and info.status == ModelStatus.LOADED
