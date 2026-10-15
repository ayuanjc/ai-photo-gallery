"""SceneAnalyzer 单元测试：缓存命中、未命中推理、超时/异常降级、白名单过滤。"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.scene.analyzer import HttpSceneInferrer, SceneAnalyzer, SceneInferrer
from app.scene.dictionary import SceneDict, load_scene_dict
from app.scene.models import SceneTags


@pytest.fixture
def scene_dict() -> SceneDict:
    return load_scene_dict()


class FakeInferrer(SceneInferrer):
    name = "fake"

    def __init__(self, scenes=None, delay: float = 0.0, fail: bool = False) -> None:
        self.scenes = list(scenes or [])
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def infer_scenes(self, asset: dict) -> list[str]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("infer boom")
        return list(self.scenes)


def _asset(aid: str, smart_tags=None, tags=None) -> dict:
    asset: dict = {"id": aid}
    if smart_tags is not None:
        asset["smartInfo"] = {"tags": smart_tags}
    if tags is not None:
        asset["tags"] = tags
    return asset


async def test_smartinfo_mapping_first_level(scene_dict):
    inferrer = FakeInferrer()
    analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer)

    tags = await analyzer.analyze(_asset("a", smart_tags=["beach", "sunset"]))

    assert inferrer.calls == 0  # 第一级命中，不触发推理
    assert tags.primary_scene == "nature"
    assert "nature" in tags.scenes
    assert "beach" in tags.sub_scenes
    assert tags.visual_attrs.get("time") == "sunset"


async def test_cache_hit_reuses_result(scene_dict):
    inferrer = FakeInferrer(["nature"])
    analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer)

    first = await analyzer.analyze(_asset("a"))
    second = await analyzer.analyze(_asset("a"))

    assert first.primary_scene == "nature"
    assert second.primary_scene == "nature"
    assert inferrer.calls == 1  # 缓存命中，仅推理一次


async def test_infer_miss_falls_back_to_second_level(scene_dict):
    inferrer = FakeInferrer(["city"])
    analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer)

    tags = await analyzer.analyze(_asset("a"))

    assert tags.scenes == ["city"]
    assert tags.primary_scene == "city"


async def test_infer_timeout_degrades_to_none(scene_dict):
    inferrer = FakeInferrer(["nature"], delay=1.0)
    analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer, infer_timeout_seconds=0.02)

    tags = await analyzer.analyze(_asset("a"))

    assert tags.primary_scene is None
    assert tags.scenes == []


async def test_infer_exception_degrades_to_none(scene_dict):
    inferrer = FakeInferrer(fail=True)
    analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer)

    tags = await analyzer.analyze(_asset("a"))

    assert tags.primary_scene is None


async def test_infer_whitelist_filtering(scene_dict):
    inferrer = FakeInferrer(["nature", "unknown_scene", "alien"])
    analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer)

    tags = await analyzer.analyze(_asset("a"))

    assert tags.scenes == ["nature"]  # 未命中白名单的标签被丢弃


async def test_visual_attr_kept_when_infer_fails(scene_dict):
    inferrer = FakeInferrer(fail=True)
    analyzer = SceneAnalyzer(scene_dict, inferrer=inferrer)

    tags = await analyzer.analyze(_asset("a", smart_tags=["sunset"]))

    assert tags.primary_scene is None
    assert tags.visual_attrs.get("time") == "sunset"


async def test_scene_tags_defaults():
    tags = SceneTags()
    assert tags.primary_scene is None
    assert tags.scenes == []
    assert tags.has_scene() is False


def _mock_client(handler) -> httpx.AsyncClient:
    """构造注入了 MockTransport 的客户端，用于替换真实网络调用。"""
    return httpx.AsyncClient(
        base_url="http://clip.test", transport=httpx.MockTransport(handler), timeout=5.0
    )


async def test_http_inferrer_returns_scenes():
    """真实 HTTP 推理器：POST {endpoint}/infer 并解析 scenes 字段。

    通过构造函数注入客户端（HttpSceneInferrer 支持 client 参数），
    避免依赖 respx 与 httpx 的版本耦合。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"scenes": ["nature", "city"]})

    client = _mock_client(handler)
    try:
        inferrer = HttpSceneInferrer("http://clip.test", client=client)
        scenes = await inferrer.infer_scenes({"id": "a"})
    finally:
        await client.aclose()

    assert scenes == ["nature", "city"]
    assert seen["method"] == "POST"
    assert seen["path"] == "/infer"


async def test_http_inferrer_error_raises():
    """上游 5xx 时抛 HTTPStatusError，交由上层做超时/降级处理。"""
    client = _mock_client(lambda request: httpx.Response(500))
    try:
        inferrer = HttpSceneInferrer("http://clip.test", client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await inferrer.infer_scenes({"id": "a"})
    finally:
        await client.aclose()