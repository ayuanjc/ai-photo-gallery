"""场景分析器：为搜索结果的每条 asset 生成多标签 SceneTags。

分级推理（避免每张图都跑大模型）：
    第一级：复用 Immich 返回的 smartInfo / tags，能映射到词典则直接使用。
    第二级：词典未命中时，用可插拔推理器做零样本判断（仅大场景）。
    第三级：TODO —— 用户查看单图详情时，再补全小场景与视觉属性。

    缓存与降级：
    - 按 asset_id 使用进程内时间衰减 LFU 缓存（TimeDecayedLFUCache）。
    - 推理超时或异常时不写缓存，返回 primary_scene=None，不阻塞搜索主链路。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod


import httpx

from ..utils.cache import TimeDecayedLFUCache
from .dictionary import SceneDict
from .models import SceneTags

logger = logging.getLogger(__name__)

# 第二级推理（无词典命中时）默认赋予的置信度。
_INFER_CONFIDENCE = 0.5


class SceneInferrer(ABC):
    """第二级推理抽象：零样本判断大场景。

    与搜索结果的具体 asset 解耦，便于替换 CLIP / 华为云等实现，并可用 respx mock。
    """

    name: str = "abstract"

    @abstractmethod
    async def infer_scenes(self, asset: dict) -> list[str]:
        """返回命中白名单的大场景 key 列表（未命中白名单的应在此过滤）。"""


class NullSceneInferrer(SceneInferrer):
    """空推理器（占位，保证未接入模型时不阻塞主链路）。

    TODO: 接入华为云图像识别 / 本地轻量 CLIP zero-shot 作为第二级实现，
    替换此占位，使无 smartInfo 标签的图片也能得到大场景。
    """

    name = "null"

    async def infer_scenes(self, asset: dict) -> list[str]:
        return []


class HttpSceneInferrer(SceneInferrer):
    """基于 HTTP 的第二级零样本推理器（生产上对接 CLIP / 华为云，可被 respx mock）。

    向推理服务 POST {endpoint}/infer，payload 携带 asset id，响应形如：
        {"scenes": ["nature", "city"]}
    """

    name = "http"

    def __init__(self, endpoint: str, client: httpx.AsyncClient | None = None) -> None:
        self._endpoint = endpoint.rstrip("/")
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient()
            self._owns_client = True

    async def infer_scenes(self, asset: dict) -> list[str]:
        resp = await self._client.post(
            f"{self._endpoint}/infer", json={"id": asset.get("id")}
        )
        resp.raise_for_status()
        return [str(s) for s in resp.json().get("scenes", [])]

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class SceneAnalyzer:
    """为 asset 生成 SceneTags，内置进程内时间衰减 LFU 缓存与超时降级。"""

    def __init__(
        self,
        dictionary: SceneDict,
        *,
        inferrer: SceneInferrer | None = None,
        cache_size: int = 2000,
        cache_halflife_seconds: float = 604800.0,
        infer_timeout_seconds: float = 3.0,
    ) -> None:
        self._dict = dictionary
        self._inferrer = inferrer or NullSceneInferrer()
        self._cache: TimeDecayedLFUCache = TimeDecayedLFUCache(
            maxsize=max(1, cache_size),
            halflife_seconds=max(1.0, cache_halflife_seconds),
        )
        self._inflight: dict[str, asyncio.Future[SceneTags]] = {}
        self._timeout = max(0.0, infer_timeout_seconds)

    async def analyze(self, asset: dict) -> SceneTags:
        """分析单条 asset，优先命中缓存，其次分级推理，超时降级。

        使用 singleflight 模式：相同 asset_id 的并发请求共享同一个推理任务，
        避免缓存 stampede（大量并发 miss 同时触发推理）。
        """
        asset_id = str(asset.get("id", "") or "")
        if asset_id:
            cached = self._cache.get(asset_id)
            if cached is not None:
                return cached
            inflight = self._inflight.get(asset_id)
            if inflight is not None and not inflight.done():
                return await inflight

        task = asyncio.ensure_future(self._compute(asset, asset_id))
        if asset_id:
            self._inflight[asset_id] = task
        try:
            return await task
        finally:
            if asset_id:
                self._inflight.pop(asset_id, None)

    async def _compute(self, asset: dict, asset_id: str) -> SceneTags:
        """实际推理逻辑（不含缓存检查），由 analyze 通过 singleflight 调度。"""
        # 第一级：基于 Immich smartInfo / tags 的确定性映射。
        base = self._analyze_smartinfo(asset)
        if base.scenes:
            self._store(asset_id, base)
            return base

        # 第二级：零样本推理大场景（带超时），保留第一级的视觉属性映射结果。
        inferred = await self._infer(asset)
        if inferred is None:
            # 降级：推理超时/异常或未命中白名单时，只返回第一级已映射的信息。
            return SceneTags(
                primary_scene=None,
                scenes=[],
                sub_scenes=base.sub_scenes,
                visual_attrs=base.visual_attrs,
                confidence=base.confidence,
            )

        merged = SceneTags(
            primary_scene=inferred.primary_scene,
            scenes=inferred.scenes,
            sub_scenes=base.sub_scenes,
            visual_attrs=base.visual_attrs,
            confidence={**base.confidence, **inferred.confidence},
        )
        self._store(asset_id, merged)
        return merged

    # ---- 第一级：smartInfo / tags 白名单映射 ----
    def _analyze_smartinfo(self, asset: dict) -> SceneTags:
        raw_tags = self._collect_tags(asset)
        return self._map_tags(raw_tags)

    @staticmethod
    def _collect_tags(asset: dict) -> list[str]:
        """从 asset 的 smartInfo.tags 与顶层 tags 收集原始标签字符串。"""
        smart_info = asset.get("smartInfo") or {}
        collected: list[str] = []
        for source in (smart_info.get("tags"), asset.get("tags")):
            if not source:
                continue
            for item in source:
                if isinstance(item, str):
                    collected.append(item)
                elif isinstance(item, dict):
                    # 兼容 Immich tag 对象的多种形态。
                    name = item.get("name") or item.get("value") or item.get("label")
                    if name:
                        collected.append(str(name))
        return collected

    def _map_tags(self, raw_tags: list[str]) -> SceneTags:
        """将原始标签归一化后映射到白名单，未命中一律丢弃。"""
        scenes: list[str] = []
        sub_scenes: list[str] = []
        visual_attrs: dict[str, str] = {}
        confidence: dict[str, float] = {}
        seen_scenes: set[str] = set()
        seen_subs: set[str] = set()

        alias_map = self._dict.scene_alias_map
        for raw in raw_tags:
            token = _normalize(raw)
            if not token:
                continue

            scene_key = alias_map.get(token)
            if scene_key is not None:
                if scene_key not in seen_scenes:
                    scenes.append(scene_key)
                    seen_scenes.add(scene_key)
                    confidence[scene_key] = 1.0
                continue

            if self._dict.is_valid_sub_scene(token):
                if token not in seen_subs:
                    sub_scenes.append(token)
                    seen_subs.add(token)
                    confidence[token] = 1.0
                parent = self._dict.parent_of(token)
                if parent is not None and parent not in seen_scenes:
                    scenes.append(parent)
                    seen_scenes.add(parent)
                    confidence[parent] = 1.0
                continue

            for category, values in self._dict.visual_attrs.items():
                if token in values and category not in visual_attrs:
                    visual_attrs[category] = token

        return SceneTags(
            primary_scene=_pick_primary(scenes, confidence),
            scenes=scenes,
            sub_scenes=sub_scenes,
            visual_attrs=visual_attrs,
            confidence=confidence,
        )

    # ---- 第二级：零样本推理（带超时降级）----
    async def _infer(self, asset: dict) -> SceneTags | None:
        """调用推理器判断大场景，超时或异常返回 None。"""
        try:
            scenes = await asyncio.wait_for(
                self._inferrer.infer_scenes(asset), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "场景推理超时（>%.1fs），降级返回，asset_id=%s",
                self._timeout,
                asset.get("id"),
            )
            return None
        except Exception as exc:  # 推理失败不应导致搜索失败
            logger.warning("场景推理失败，asset_id=%s err=%s", asset.get("id"), exc)
            return None

        valid_scenes: list[str] = []
        confidence: dict[str, float] = {}
        for key in scenes or []:
            key = _normalize(key)
            if self._dict.is_valid_scene(key) and key not in valid_scenes:
                valid_scenes.append(key)
                confidence[key] = _INFER_CONFIDENCE

        if not valid_scenes:
            return None  # 未命中白名单视为无结果，不写缓存
        return SceneTags(
            primary_scene=_pick_primary(valid_scenes, confidence),
            scenes=valid_scenes,
            confidence=confidence,
        )

    def _store(self, asset_id: str, tags: SceneTags) -> None:
        if asset_id:
            self._cache[asset_id] = tags


def _normalize(token: str) -> str:
    """归一化自由文本标签：小写 + 连字符/空格统一为下划线。"""
    return token.strip().lower().replace("-", "_").replace(" ", "_")


def _pick_primary(scenes: list[str], confidence: dict[str, float]) -> str | None:
    """从大场景列表中挑选主场景：置信度最高者优先，否则取首个。"""
    if not scenes:
        return None
    if confidence:
        return max(scenes, key=lambda s: confidence.get(s, 0.0))
    return scenes[0]