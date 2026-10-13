"""运行时功能开关配置。

提供管理页面可动态切换的功能开关层：
- 服务读取开关时调用 runtime_enabled(key, settings_default)：用户显式修改过则用
  运行时值，否则回退 Settings 默认值（保证测试中 Settings(dedup_enabled=False)
  等构造方式不受影响）。
- 修改写入内存 + JSON 文件持久化（重启后覆盖值继续生效）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from pathlib import Path
from typing import Any

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
_PERSIST_PATH = _DATA_DIR / "runtime_config.json"

FEATURES_META: dict[str, dict[str, str | bool]] = {
    "scene_enabled": {
        "name": "场景分析",
        "description": "搜索结果的多标签场景标注（大场景/小场景/视觉属性）",
        "requires_restart": False,
    },
    "stats_enabled": {
        "name": "统计大屏",
        "description": "地图足迹 / EXIF 分布 / 器材偏好统计接口",
        "requires_restart": False,
    },
    "auth_enabled": {
        "name": "API 鉴权",
        "description": "搜索接口 x-api-key 校验（需已配置 SEARCH_API_KEY）",
        "requires_restart": False,
    },
    "rate_limit_enabled": {
        "name": "接口限流",
        "description": "搜索接口令牌桶限流",
        "requires_restart": False,
    },
    "dedup_enabled": {
        "name": "结果去重",
        "description": "搜索结果时间窗口去重",
        "requires_restart": False,
    },
    "explain_enabled": {
        "name": "排序可解释性",
        "description": "返回排序解释信息（为什么排这里）",
        "requires_restart": False,
    },
    "image_query_enabled": {
        "name": "以图搜图",
        "description": "参考图语义搜索增强",
        "requires_restart": False,
    },
    "ai_enabled": {
        "name": "AI 分析",
        "description": "同一个人 vs 相似图片判定接口",
        "requires_restart": False,
    },
    "feedback_enabled": {
        "name": "搜索反馈",
        "description": "用户点击/跳过反馈记录，供排序微加权",
        "requires_restart": False,
    },
    "suggest_enabled": {
        "name": "搜索建议",
        "description": "基于场景词典与热门词的前缀/包含匹配建议",
        "requires_restart": False,
    },
    "correct_enabled": {
        "name": "搜索纠错",
        "description": "基于编辑距离的场景词典纠错匹配",
        "requires_restart": False,
    },
    "huawei_enabled": {
        "name": "华为云图像识别",
        "description": "视觉识别 provider（重启生效）",
        "requires_restart": True,
    },
    "huawei_face_enabled": {
        "name": "华为云人脸识别 FRS",
        "description": "人脸分类功能（重启生效）",
        "requires_restart": True,
    },
}


class RuntimeConfig:
    """运行时功能开关：内存持有 + JSON 持久化 + asyncio 锁保护。"""

    def __init__(self, settings: Settings, persist_path: Path | None = None) -> None:
        self._persist_path = persist_path or _PERSIST_PATH
        self._overrides: dict[str, bool] = {}
        self._lock = asyncio.Lock()
        self._defaults: dict[str, bool] = {
            "scene_enabled": settings.scene_enabled,
            "stats_enabled": getattr(settings, "stats_enabled", True),
            "auth_enabled": bool(settings.search_api_key),
            "rate_limit_enabled": True,
            "dedup_enabled": settings.dedup_enabled,
            "explain_enabled": settings.explain_enabled,
            "image_query_enabled": settings.image_query_enabled,
            "ai_enabled": True,
            "feedback_enabled": True,
            "suggest_enabled": True,
            "correct_enabled": True,
            "huawei_enabled": settings.huawei_enabled,
            "huawei_face_enabled": settings.huawei_face_enabled,
        }
        self._load()

    def _load(self) -> None:
        try:
            if self._persist_path.exists():
                data = json.loads(self._persist_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._overrides = {
                        k: bool(v) for k, v in data.items() if k in FEATURES_META
                    }
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("运行时配置加载失败，使用默认值: %s", exc)
            self._overrides = {}

    async def save(self) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(
                json.dumps(self._overrides, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("运行时配置持久化失败: %s", exc)

    def is_enabled(self, key: str, settings_default: bool) -> bool:
        if key in self._overrides:
            return self._overrides[key]
        return settings_default

    def current_value(self, key: str) -> bool:
        if key in self._overrides:
            return self._overrides[key]
        return self._defaults.get(key, False)

    async def set(self, key: str, value: bool) -> None:
        if key not in FEATURES_META:
            raise KeyError(key)
        async with self._lock:
            self._overrides[key] = bool(value)
            await self.save()

    async def set_many(self, updates: dict[str, bool]) -> None:
        unknown = [k for k in updates if k not in FEATURES_META]
        if unknown:
            raise KeyError(",".join(unknown))
        async with self._lock:
            for k, v in updates.items():
                self._overrides[k] = bool(v)
            await self.save()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        for key, meta in FEATURES_META.items():
            items[key] = {
                "name": meta["name"],
                "description": meta["description"],
                "requires_restart": meta["requires_restart"],
                "enabled": self.current_value(key),
                "overridden": key in self._overrides,
            }
        return items

    def overridden_snapshot(self) -> dict[str, bool]:
        return dict(self._overrides)


_runtime_config: RuntimeConfig | None = None


def get_runtime_config() -> RuntimeConfig:
    global _runtime_config
    if _runtime_config is None:
        _runtime_config = RuntimeConfig(get_settings())
    return _runtime_config


def runtime_enabled(key: str, settings_default: bool) -> bool:
    """读取运行时开关：用户显式修改过则用运行时值，否则用 Settings 默认值。"""
    return get_runtime_config().is_enabled(key, settings_default)


def reset_runtime_config() -> None:
    global _runtime_config
    _runtime_config = None
