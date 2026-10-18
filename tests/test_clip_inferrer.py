"""CLIP 零样本场景推理器测试（respx mock，不联网）。"""
from __future__ import annotations

import json
import math

import httpx
import pytest
import respx

from app.core.config import Settings
from app.scene.analyzer import SceneAnalyzer, SceneInferrer
from app.scene.clip_inferrer import ClipSceneInferrer, _sim_to_confidence
from app.scene.dictionary import get_scene_dict
from app.scene.models import SceneTags

ML = "http://ml.test:3003"
IMMICH = "http://immich.test:3200"


def _emb(seed: float, dims: int = 512) -> list[float]:
    """构造确定性伪向量（同 seed 相同方向）。"""
    vec = [math.sin(seed + i * 0.01) for i in range(dims)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _clip_response(emb: list[float]) -> dict:
    return {"clip": json.dumps([round(x, 6) for x in emb])}


def _unit(cos_value: float) -> list[float]:
    """构造与 [1,0,0...] 恰好成 cos_value 余弦的单位向量。"""
    vec = [cos_value, math.sqrt(max(0.0, 1.0 - cos_value * cos_value))]
    vec += [0.0] * (512 - len(vec))
    return vec


def _make_inferrer(**kwargs) -> ClipSceneInferrer:
    defaults = dict(
        endpoint=ML,
        model_name="ViT-B-32__openai",
        immich_base_url=IMMICH,
        immich_api_key="test-key",
        dictionary=get_scene_dict(),
        client=httpx.AsyncClient(trust_env=False),
    )
    defaults.update(kwargs)
    return ClipSceneInferrer(**defaults)


@pytest.fixture
def scene_dict():
    return get_scene_dict()


class TestSimToConfidence:
    def test_bounds(self):
        assert _sim_to_confidence(0.1) == 0.5
        assert _sim_to_confidence(0.9) == 0.95
        # 校准区间 sim=0.22 → 0.5，sim=0.30 → 0.95（按 ViT-B-32 实测分布）
        assert _sim_to_confidence(0.22) == 0.5
        assert _sim_to_confidence(0.30) == 0.95
        mid = _sim_to_confidence(0.26)
        assert 0.5 < mid < 0.95


class TestClipSceneInferrer:
    @respx.mock
    async def test_infer_full_returns_analyzed_tags(self):
        inferrer = _make_inferrer()
        beach = _emb(1.0)
        with respx.mock:
            # 预热阶段：所有文本提示词返回 beach 向量（保证可解析）
            respx.post(f"{ML}/predict").mock(
                return_value=httpx.Response(200, json=_clip_response(beach))
            )
            respx.get(f"{IMMICH}/api/assets/a1/thumbnail").mock(
                return_value=httpx.Response(200, content=b"fake-webp")
            )
            tags = await inferrer.infer_full({"id": "a1"})

        # 所有提示词同向量 → 相似度 1.0 ≥ 阈值 → nature + beach 均命中
        assert tags is not None
        assert tags.source == "analyzed"
        assert "nature" in tags.scenes
        assert "beach" in tags.sub_scenes
        assert tags.primary_scene == "nature"
        assert 0.5 <= tags.confidence["nature"] <= 0.95

    @respx.mock
    async def test_infer_scenes_compat(self):
        inferrer = _make_inferrer()
        with respx.mock:
            respx.post(f"{ML}/predict").mock(
                return_value=httpx.Response(200, json=_clip_response(_emb(1.0)))
            )
            respx.get(f"{IMMICH}/api/assets/a1/thumbnail").mock(
                return_value=httpx.Response(200, content=b"fake-webp")
            )
            scenes = await inferrer.infer_scenes({"id": "a1"})
        assert "nature" in scenes

    @respx.mock
    async def test_thumbnail_failure_returns_none(self):
        inferrer = _make_inferrer()
        with respx.mock:
            respx.post(f"{ML}/predict").mock(
                return_value=httpx.Response(200, json=_clip_response(_emb(1.0)))
            )
            respx.get(f"{IMMICH}/api/assets/a1/thumbnail").mock(
                return_value=httpx.Response(404)
            )
            tags = await inferrer.infer_full({"id": "a1"})
        assert tags is None

    @respx.mock
    async def test_ml_failure_returns_none(self):
        inferrer = _make_inferrer()
        with respx.mock:
            respx.post(f"{ML}/predict").mock(side_effect=httpx.ConnectError("down"))
            respx.get(f"{IMMICH}/api/assets/a1/thumbnail").mock(
                return_value=httpx.Response(200, content=b"fake-webp")
            )
            tags = await inferrer.infer_full({"id": "a1"})
        assert tags is None

    @respx.mock
    async def test_no_asset_id_returns_none(self):
        inferrer = _make_inferrer()
        assert await inferrer.infer_full({}) is None
        assert await inferrer.infer_full({"id": ""}) is None

    @respx.mock
    async def test_low_similarity_returns_none(self):
        inferrer = _make_inferrer()
        # 预热：文本向量与图片向量正交（不同 seed）→ 相似度极低
        with respx.mock:
            # 文本预热阶段返回 seed=2.0 向量
            text_route = respx.post(f"{ML}/predict").mock(
                return_value=httpx.Response(200, json=_clip_response(_emb(2.0)))
            )
            respx.get(f"{IMMICH}/api/assets/a1/thumbnail").mock(
                return_value=httpx.Response(200, content=b"fake-webp")
            )
            await inferrer.warmup()
            # 图片 embedding 用同向量但整体乘 -1（余弦 = -1，必然低于阈值）
            neg = [-x for x in _emb(2.0)]
            text_route.mock(
                return_value=httpx.Response(200, json=_clip_response(neg))
            )
            tags = await inferrer.infer_full({"id": "a1"})
        assert tags is None

    async def test_classify_pure(self):
        inferrer = _make_inferrer()
        emb = _emb(3.0)
        # 手动塞入提示词矩阵：beach 相似度 1.0，其余 0
        beach_vec = emb
        inferrer._prompts = {
            "beach": [beach_vec],
            "mountain": [[-x for x in beach_vec]],
            "nature": [beach_vec],
            "city": [[-x for x in beach_vec]],
        }
        tags = inferrer._classify(emb)
        assert tags is not None
        assert tags.source == "analyzed"
        assert tags.primary_scene == "nature"
        assert "beach" in tags.sub_scenes
        assert "nature" in tags.scenes
        assert "city" not in tags.scenes

    async def test_classify_max_scenes_limit(self):
        inferrer = _make_inferrer(max_scenes=2)
        emb = _emb(4.0)
        inferrer._prompts = {
            "nature": [emb],
            "city": [emb],
            "people": [emb],
            "life": [emb],
        }
        tags = inferrer._classify(emb)
        assert tags is not None
        assert len(tags.scenes) == 2


class TestAnalyzerIntegration:
    @respx.mock
    async def test_analyzer_uses_infer_full(self, scene_dict):
        """analyzer 第二级优先调 infer_full，结果 source=analyzed 并写缓存。"""
        inferrer = _make_inferrer(dictionary=scene_dict)
        with respx.mock:
            respx.post(f"{ML}/predict").mock(
                return_value=httpx.Response(200, json=_clip_response(_emb(5.0)))
            )
            respx.get(f"{IMMICH}/api/assets/a1/thumbnail").mock(
                return_value=httpx.Response(200, content=b"fake-webp")
            )
            analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer, cache_size=10)
            tags = await analyzer.analyze({"id": "a1"})

        assert tags.primary_scene is not None
        assert tags.source == "analyzed"
        assert tags.scenes

    async def test_inferred_sub_scenes_survive_merge(self, scene_dict):
        """回归：_compute 合并两级结果时不得丢弃 infer_full 的子场景。"""

        class StubInferrer(SceneInferrer):
            name = "stub"

            async def infer_scenes(self, asset):
                return []

            async def infer_full(self, asset):
                return SceneTags(
                    primary_scene="animal",
                    scenes=["animal"],
                    sub_scenes=["cat"],
                    visual_attrs={},
                    confidence={"animal": 0.7, "cat": 0.8},
                    source="analyzed",
                )

        analyzer = SceneAnalyzer(scene_dict, inferrer=StubInferrer(), cache_size=10)
        tags = await analyzer.analyze({"id": "a1"})
        assert tags.scenes == ["animal"]
        assert tags.sub_scenes == ["cat"]
        assert tags.confidence.get("cat") == 0.8
        assert tags.source == "analyzed"

    async def test_warmup_failure_then_lazy_retry(self):
        """预热失败后 _ensure_prompts 应抛错（由调用方降级），不静默自愈为空。"""
        inferrer = _make_inferrer()
        with respx.mock:
            respx.post(f"{ML}/predict").mock(
                side_effect=httpx.ConnectError("down")
            )
            with pytest.raises((httpx.ConnectError, RuntimeError)):
                await inferrer.warmup()
            assert inferrer._prompts is None

class TestTighteningGuards:
    """T5 收紧规则守卫：from_settings 字段名回归 + min_conf 过滤 + top1 跟随子场景。"""

    def test_from_settings_reads_all_scene_fields(self):
        """回归：from_settings 引用不存在的 Settings 字段会在启动即崩。"""
        settings = Settings(
            scene_inference_endpoint="http://ml2:3003/",
            scene_clip_model="my-model",
            scene_clip_scene_threshold=0.21,
            scene_clip_sub_scene_threshold=0.22,
            scene_min_confidence=0.71,
            scene_max_scenes=3,
            scene_max_sub_scenes=4,
        )
        inferrer = ClipSceneInferrer.from_settings(settings, get_scene_dict())
        assert inferrer._endpoint == "http://ml2:3003"
        assert inferrer._model == "my-model"
        assert inferrer._scene_threshold == 0.21
        assert inferrer._sub_threshold == 0.22
        assert inferrer._min_conf == 0.71
        assert inferrer._max_scenes == 3
        assert inferrer._max_subs == 4

    async def test_classify_min_confidence_filters_below_floor(self):
        """相似度过阈值但置信度低于 min_confidence 的标签必须丢弃。"""
        inferrer = _make_inferrer(
            scene_threshold=0.22, sub_scene_threshold=0.22, min_confidence=0.60
        )
        emb = _unit(1.0)
        inferrer._prompts = {
            "nature": [_unit(0.23)],
            "beach": [_unit(0.23)],
            "city": [_unit(0.30)],
            "street": [_unit(0.30)],
        }
        tags = inferrer._classify(emb)
        assert tags is not None
        assert "nature" not in tags.scenes
        assert "beach" not in tags.sub_scenes
        assert tags.primary_scene == "city"
        assert "street" in tags.sub_scenes
        assert tags.confidence["street"] == pytest.approx(_sim_to_confidence(0.30))

    async def test_classify_top_scene_follows_top_sub(self):
        """max_scenes=1 时，大场景 top-1 必须跟随最高置信子场景的父场景。"""
        inferrer = _make_inferrer(max_scenes=1)
        emb = _unit(1.0)
        inferrer._prompts = {
            "city": [_unit(0.90)],
            "nature": [_unit(0.80)],
            "mountain": [_unit(0.99)],
            "beach": [_unit(0.81)],
        }
        tags = inferrer._classify(emb)
        assert tags is not None
        assert tags.primary_scene == "nature"
        assert tags.scenes == ["nature"]
        assert "city" not in tags.scenes
        assert tags.sub_scenes == ["mountain", "beach"]

    async def test_classify_max_sub_scenes_limit(self):
        inferrer = _make_inferrer(max_sub_scenes=1)
        emb = _unit(1.0)
        inferrer._prompts = {
            "nature": [_unit(0.90)],
            "mountain": [_unit(0.99)],
            "beach": [_unit(0.81)],
        }
        tags = inferrer._classify(emb)
        assert tags is not None
        assert tags.sub_scenes == ["mountain"]

class TestThumbnailRetry:
    """新上传图缩略图未就绪（404）时的退避重试。"""

    async def test_retry_until_thumbnail_ready(self, monkeypatch):
        """前两次 404、第三次 200：必须重试拿到缩略图并完成推理。"""
        inferrer = _make_inferrer()
        inferrer._prompts = {"nature": [_unit(0.30)]}
        calls = {"n": 0}

        async def fake_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("app.scene.clip_inferrer.asyncio.sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(404)
            return httpx.Response(200, content=b"fake-webp")

        with respx.mock:
            respx.get(f"{IMMICH}/api/assets/a1/thumbnail").mock(side_effect=handler)
            emb = await inferrer._download_thumbnail("a1")
        assert emb == b"fake-webp"
        assert calls["n"] == 3

    async def test_give_up_after_max_retries(self, monkeypatch):
        """恒 404：最多尝试 5 次后放弃返回 None。"""
        inferrer = _make_inferrer()
        calls = {"n": 0}

        async def fake_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("app.scene.clip_inferrer.asyncio.sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404)

        with respx.mock:
            respx.get(f"{IMMICH}/api/assets/a2/thumbnail").mock(side_effect=handler)
            emb = await inferrer._download_thumbnail("a2")
        assert emb is None
        assert calls["n"] == 5

    async def test_no_retry_on_non_404(self):
        """500 等非 404 错误：立即放弃，不重试。"""
        inferrer = _make_inferrer()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500)

        with respx.mock:
            respx.get(f"{IMMICH}/api/assets/a3/thumbnail").mock(side_effect=handler)
            emb = await inferrer._download_thumbnail("a3")
        assert emb is None
        assert calls["n"] == 1
