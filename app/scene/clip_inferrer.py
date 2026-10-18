"""基于 Immich ML 容器 CLIP 的零样本场景推理器。

复用 Immich 已部署的 machine-learning 服务（ViT-B-32__openai）：
    - 图片侧：下载 asset 缩略图 → POST /predict (clip.visual) → 图片 embedding。
    - 文本侧：启动预热时为词典场景提示词批量取文本 embedding。
    - 推理：图片 embedding 与各提示词 embedding 的余弦相似度，超过阈值即命中。

协议（v3.x ML 容器，FastAPI /predict multipart）：
    entries={"clip":{"visual"|"textual":{"modelName":...}}} + image 文件 / text 字段
    响应 {"clip": "<JSON 字符串的 512 维向量>", "imageWidth": .., "imageHeight": ..}

该推理结果为真实模型输出（source=analyzed），允许进入写回链路。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math

import httpx

from ..core.config import Settings
from .analyzer import SceneInferrer
from .dictionary import SceneDict
from .models import SceneTags

logger = logging.getLogger(__name__)

# 相似度 → 置信度的线性映射参数（sim=0.22 → 0.5，sim=0.30 → 0.95）。
# 区间按 ViT-B-32 零样本实测分布校准（40 样本 top1 sim 0.230~0.281，中位 0.260）。
_CONF_MIN_SIM = 0.22
_CONF_MAX_SIM = 0.30
_CONF_MIN = 0.5
_CONF_MAX = 0.95

# 预热时文本 embedding 的并发上限。
_WARMUP_CONCURRENCY = 8

# 场景提示词表：词典 key → 英文 CLIP 提示词（同 key 多提示词取最大相似度）。
SCENE_PROMPTS: dict[str, list[str]] = {
    # ---- 大场景 ----
    "nature": ["a photo of natural scenery", "a landscape photo of nature"],
    "city": ["a photo of a city skyline", "a photo of an urban street"],
    "people": ["a photo of a person", "a photo of people together"],
    "life": ["a photo of daily life", "a photo of food and everyday moments"],
    "event": ["a photo of a celebration event", "a photo from a trip or party"],
    "animal": ["a photo of an animal", "a wildlife photo"],
    "architecture": ["a photo of architecture", "a photo of a building"],
    "night": ["a photo taken at night", "a photo of night lights"],
    "vehicle": ["a photo of a vehicle", "a photo of a car or train"],
    "art": ["a photo of art", "a photo of a painting or artwork"],
    # ---- nature 子场景 ----
    "beach": ["a photo of a beach", "a sandy beach by the ocean"],
    "mountain": ["a photo of mountains", "snowy mountain peaks"],
    "forest": ["a photo of a forest", "trees in a forest"],
    "lake": ["a photo of a lake", "a calm lake with mountains"],
    "desert": ["a photo of a desert", "sand dunes in the desert"],
    "snow": ["a photo of snow", "a snowy landscape"],
    "ocean": ["a photo of the ocean", "sea waves on the beach"],
    "sky": ["a photo of the sky", "a photo of clouds and sunset"],
    "flower": ["a photo of flowers", "blooming flowers close-up"],
    # ---- city 子场景 ----
    "street": ["a photo of a city street", "people walking on a street"],
    "building": ["a photo of tall buildings", "skyscrapers in the city"],
    "night_view": ["a photo of a city at night", "city lights at night"],
    "traffic": ["a photo of traffic", "cars on a busy road"],
    "landmark": ["a photo of a famous landmark", "a tourist landmark"],
    # ---- people 子场景 ----
    "single": ["a photo of one person alone"],
    "group": ["a group photo of many people"],
    "wedding": ["a wedding photo", "a bride in a wedding dress"],
    "party": ["a photo of a party", "friends at a party"],
    "child": ["a photo of a child", "kids playing"],
    "portrait": ["a portrait photo of a face", "a close-up portrait"],
    "family": ["a family photo", "parents and children together"],
    # ---- life 子场景 ----
    "food": ["a photo of food", "a delicious meal on a table"],
    "pet": ["a photo of a pet at home"],
    "home": ["a photo of a home interior", "a cozy living room"],
    "office": ["a photo of an office workspace", "a desk with a laptop"],
    "sport": ["a photo of sports", "people exercising and running"],
    # ---- event 子场景 ----
    "festival": ["a photo of a festival", "a festival celebration with lights"],
    "celebration": ["a photo of a celebration", "a birthday celebration"],
    "performance": ["a photo of a stage performance", "a concert performance"],
    "travel": ["a travel photo, sightseeing", "tourists visiting a scenic spot"],
    "graduation": ["a graduation ceremony photo", "graduates in gowns"],
    # ---- animal 子场景 ----
    "dog": ["a photo of a dog", "a cute puppy"],
    "cat": ["a photo of a cat", "a cute kitten"],
    "bird": ["a photo of a bird", "a bird flying"],
    "wildlife": ["a photo of wild animals", "a wild animal in nature"],
    "marine": ["a photo of sea animals", "dolphins in the ocean"],
    "insect": ["a photo of an insect", "a butterfly close-up"],
    # ---- architecture 子场景 ----
    "temple": ["a photo of a temple", "an ancient temple or church"],
    "bridge": ["a photo of a bridge", "a bridge over a river"],
    "modern": ["a photo of modern architecture", "a modern glass building"],
    "historic": ["a photo of a historic building", "an ancient palace"],
    "interior": ["a photo of a room interior", "an indoor space design"],
    # ---- night 子场景 ----
    "starry": ["a photo of a starry night sky", "the milky way at night"],
    "city_lights": ["a photo of city lights at night", "neon city skyline at night"],
    "moon": ["a photo of the moon", "a full moon in the night sky"],
    "fireworks": ["a photo of fireworks", "fireworks in the night sky"],
    "neon": ["a photo of neon signs at night", "neon lights on a street"],
    # ---- vehicle 子场景 ----
    "car": ["a photo of a car", "a car on the road"],
    "bike": ["a photo of a bicycle", "a person riding a bike"],
    "boat": ["a photo of a boat", "a boat on the water"],
    "plane": ["a photo of an airplane", "a plane in the sky"],
    "train": ["a photo of a train", "a train on the tracks"],
    # ---- art 子场景 ----
    "painting": ["a photo of a painting", "an oil painting artwork"],
    "sculpture": ["a photo of a sculpture", "a statue sculpture"],
    "graffiti": ["a photo of graffiti", "colorful graffiti on a wall"],
    "design": ["a graphic design artwork"],
    "craft": ["a photo of handmade crafts"],
}


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（输入为等长向量）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _sim_to_confidence(sim: float) -> float:
    """CLIP 相似度映射为置信度（0.25→0.5，0.475→0.95，区间外截断）。"""
    if sim <= _CONF_MIN_SIM:
        return _CONF_MIN
    if sim >= _CONF_MAX_SIM:
        return _CONF_MAX
    ratio = (sim - _CONF_MIN_SIM) / (_CONF_MAX_SIM - _CONF_MIN_SIM)
    return round(_CONF_MIN + ratio * (_CONF_MAX - _CONF_MIN), 4)


class ClipSceneInferrer(SceneInferrer):
    """零样本 CLIP 场景推理器（复用 Immich machine-learning 容器）。

    - 大场景与子场景均由 CLIP 相似度判定，结果为真实模型输出（source=analyzed）。
    - 提示词文本 embedding 在首次推理前懒加载预热（可显式调 warmup）。
    - 缩略图下载与推理失败一律返回 None，不阻塞主链路。
    """

    name = "clip"

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        immich_base_url: str,
        immich_api_key: str,
        dictionary: SceneDict,
        *,
        scene_threshold: float = 0.30,
        sub_scene_threshold: float = 0.32,
        min_confidence: float = 0.60,
        max_scenes: int = 1,
        max_sub_scenes: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model_name
        self._immich_base = immich_base_url.rstrip("/")
        self._immich_key = immich_api_key
        self._dict = dictionary
        self._scene_threshold = scene_threshold
        self._sub_threshold = sub_scene_threshold
        # 置信度下限：低于该值的标签不返回、不展示、不写回（多场景混标即噪声）。
        self._min_conf = min_confidence
        self._max_scenes = max(1, max_scenes)
        self._max_subs = max(1, max_sub_scenes)
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0), trust_env=False
            )
            self._owns_client = True
        self._lock = asyncio.Lock()
        self._prompts: dict[str, list[list[float]]] | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, dictionary: SceneDict
    ) -> ClipSceneInferrer:
        """按 Settings 便捷构造（endpoint 即 ML 服务根地址）。"""
        return cls(
            endpoint=settings.scene_inference_endpoint,
            model_name=settings.scene_clip_model,
            immich_base_url=settings.immich_base_url,
            immich_api_key=settings.immich_api_key,
            dictionary=dictionary,
            scene_threshold=settings.scene_clip_scene_threshold,
            sub_scene_threshold=settings.scene_clip_sub_scene_threshold,
            min_confidence=settings.scene_min_confidence,
            max_scenes=settings.scene_max_scenes,
            max_sub_scenes=settings.scene_max_sub_scenes,
        )

    async def warmup(self) -> None:
        """预计算全部提示词文本 embedding（幂等，失败抛出由调用方记录）。"""
        await self._ensure_prompts()

    async def infer_scenes(self, asset: dict) -> list[str]:
        """兼容旧接口：仅返回大场景 key 列表。"""
        tags = await self.infer_full(asset)
        return tags.scenes if tags is not None else []

    async def infer_full(self, asset: dict) -> SceneTags | None:
        """完整零样本推理：返回带子场景与置信度的 SceneTags（source=analyzed）。"""
        asset_id = str(asset.get("id", "") or "")
        if not asset_id:
            return None
        try:
            await self._ensure_prompts()
            image_emb = await self._embed_image_for_asset(asset_id)
            if image_emb is None:
                return None
            return self._classify(image_emb)
        except Exception as exc:
            logger.warning("CLIP 场景推理失败 asset_id=%s err=%s", asset_id, exc)
            return None

    # ---- 内部实现 ----
    def _classify(self, image_emb: list[float]) -> SceneTags | None:
        """图片 embedding 与提示词矩阵比对，组装 SceneTags。

        收紧规则（多场景混标即噪声）：
        - 大场景只保留 top-N（默认 1），子场景 top-M（默认 2）；
        - 置信度低于 min_confidence 的标签一律丢弃（不展示、不写回）；
        - 最高置信子场景的父大场景插到首位（保证 primary 与子场景一致），
          再截断到 max_scenes。
        """
        assert self._prompts is not None
        sims: dict[str, float] = {}
        for key, embeddings in self._prompts.items():
            sims[key] = max(_cosine(image_emb, emb) for emb in embeddings)

        scene_items = sorted(
            (
                (k, v)
                for k, v in sims.items()
                if self._dict.is_valid_scene(k)
                and v >= self._scene_threshold
                and _sim_to_confidence(v) >= self._min_conf
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )
        sub_items = sorted(
            (
                (k, v)
                for k, v in sims.items()
                if self._dict.is_valid_sub_scene(k)
                and v >= self._sub_threshold
                and _sim_to_confidence(v) >= self._min_conf
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )

        top_scenes = [k for k, _ in scene_items[: self._max_scenes]]
        top_subs = [k for k, _ in sub_items[: self._max_subs]]

        # 最高置信子场景的父大场景置首（子场景达标，父场景必然相关），
        # 随后截断，保证不超出 max_scenes。
        if top_subs:
            parent = self._dict.parent_of(top_subs[0])
            if parent is not None:
                if parent not in top_scenes:
                    top_scenes.insert(0, parent)
                else:
                    top_scenes.remove(parent)
                    top_scenes.insert(0, parent)
                top_scenes = top_scenes[: self._max_scenes]

        if not top_scenes:
            return None

        confidence = {
            **{k: _sim_to_confidence(sims[k]) for k in top_subs},
            **{k: _sim_to_confidence(sims[k]) for k in top_scenes if k in sims},
        }
        primary = top_scenes[0]
        return SceneTags(
            primary_scene=primary,
            scenes=top_scenes,
            sub_scenes=top_subs,
            visual_attrs={},
            confidence=confidence,
            source="analyzed",
        )

    async def _ensure_prompts(self) -> None:
        """懒加载提示词文本 embedding（双重检查 + 锁，预热失败下次重试）。"""
        if self._prompts is not None:
            return
        async with self._lock:
            if self._prompts is not None:
                return
            sem = asyncio.Semaphore(_WARMUP_CONCURRENCY)

            async def _one(prompt: str) -> list[float] | None:
                async with sem:
                    return await self._embed_text(prompt)

            matrix: dict[str, list[list[float]]] = {}
            for key, prompts in SCENE_PROMPTS.items():
                embeddings = [e for e in await asyncio.gather(*(_one(p) for p in prompts)) if e]
                if embeddings:
                    matrix[key] = embeddings
            if not matrix:
                raise RuntimeError("CLIP 提示词预热全部失败，请检查 ML 服务")
            self._prompts = matrix
            logger.info("CLIP 场景提示词预热完成: %d 个 key", len(matrix))

    async def _embed_text(self, text: str) -> list[float] | None:
        entries = json.dumps({"clip": {"textual": {"modelName": self._model}}})
        resp = await self._client.post(
            f"{self._endpoint}/predict",
            data={"entries": entries, "text": text},
        )
        resp.raise_for_status()
        return self._parse_embedding(resp.json())

    async def _embed_image_for_asset(self, asset_id: str) -> list[float] | None:
        thumb = await self._download_thumbnail(asset_id)
        if thumb is None:
            return None
        entries = json.dumps({"clip": {"visual": {"modelName": self._model}}})
        resp = await self._client.post(
            f"{self._endpoint}/predict",
            data={"entries": entries},
            files={"image": ("thumb", thumb, "image/webp")},
        )
        resp.raise_for_status()
        return self._parse_embedding(resp.json())

    async def _download_thumbnail(self, asset_id: str) -> bytes | None:
        """下载缩略图；404（新上传缩略图未生成）时指数退避重试。

        Immich 上传后缩略图生成存在亚秒级延迟，AssetCreate 事件先于
        缩略图就绪到达，不重试会导致新图永远打不上标签。
        """
        delay = 0.5
        for attempt in range(5):
            resp = await self._client.get(
                f"{self._immich_base}/api/assets/{asset_id}/thumbnail",
                headers={"x-api-key": self._immich_key},
            )
            if resp.status_code == 200:
                return resp.content
            if resp.status_code != 404:
                logger.debug(
                    "缩略图下载失败 asset_id=%s status=%s", asset_id, resp.status_code
                )
                return None
            if attempt < 4:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
        logger.debug("缩略图重试后仍不可用 asset_id=%s", asset_id)
        return None

    @staticmethod
    def _parse_embedding(payload: dict) -> list[float] | None:
        """解析 ML 响应：clip 值为 JSON 字符串形式的向量。"""
        raw = (payload or {}).get("clip")
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if not isinstance(raw, list) or not raw:
            return None
        return [float(x) for x in raw]

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
