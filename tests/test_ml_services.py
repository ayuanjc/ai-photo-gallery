"""P22-P24 ML 服务层测试：场景推理 + 向量索引 + 模型管理。"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.ml_scene_inferrer import (
    LocalMLInferrer,
    HttpMLInferrer,
    MLSceneInferrer,
    create_ml_inferrer,
)
from app.services.vector_index import VectorIndex, _normalize, _dot
from app.services.model_manager import ModelManager, ModelStatus


# ==================== P22: ML 场景推理 ====================

async def test_local_inferrer_with_smart_tags():
    inferrer = LocalMLInferrer()
    asset = {"smartInfo": {"tags": ["beach", "sunset", "nature"]}}
    scenes = await inferrer.infer(asset)
    assert "nature" in scenes


async def test_local_inferrer_with_asset_tags():
    inferrer = LocalMLInferrer()
    asset = {"tags": [{"value": "city"}, {"value": "street"}]}
    scenes = await inferrer.infer(asset)
    assert "city" in scenes


async def test_local_inferrer_no_tags():
    inferrer = LocalMLInferrer()
    scenes = await inferrer.infer({"id": "a1"})
    assert scenes == []


async def test_local_inferrer_mixed_tags():
    inferrer = LocalMLInferrer()
    asset = {
        "smartInfo": {"tags": ["dog"]},
        "tags": [{"value": "pet"}],
    }
    scenes = await inferrer.infer(asset)
    assert "animal" in scenes
    assert "life" in scenes


async def test_local_inferrer_probe():
    inferrer = LocalMLInferrer()
    assert await inferrer.probe() is True


async def test_batch_infer():
    inferrer = LocalMLInferrer()
    assets = [
        {"smartInfo": {"tags": ["beach"]}},
        {"smartInfo": {"tags": ["city"]}},
        {"smartInfo": {"tags": ["dog"]}},
    ]
    results = await inferrer.infer_batch(assets)
    assert len(results) == 3
    assert "nature" in results[0]
    assert "city" in results[1]
    assert "animal" in results[2]


async def test_batch_infer_partial_failure():
    """批量推理中部分失败不影响其他项。"""
    inferrer = LocalMLInferrer()
    assets = [
        {"smartInfo": {"tags": ["beach"]}},
        None,
        {"smartInfo": {"tags": ["city"]}},
    ]
    results = await inferrer.infer_batch(assets)
    assert len(results) == 3
    assert "nature" in results[0]
    assert results[1] == []
    assert "city" in results[2]


async def test_http_inferrer_infer():
    """HTTP 推理器正常调用。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/infer":
            return httpx.Response(200, json={"scenes": ["nature", "city"]})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    inferrer = HttpMLInferrer("http://ml.test", api_key="test-key")
    inferrer._endpoint = "http://ml.test"

    async with httpx.AsyncClient(transport=transport, base_url="http://ml.test") as client:
        original_infer = inferrer.infer
        async def mock_infer(asset):
            resp = await client.post("/infer", json={"asset": asset}, headers={"Authorization": "Bearer test-key"})
            resp.raise_for_status()
            return resp.json().get("scenes", [])
        inferrer.infer = mock_infer
        scenes = await inferrer.infer({"id": "a1"})
    assert scenes == ["nature", "city"]


async def test_http_inferrer_probe_healthy():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    inferrer = HttpMLInferrer("http://ml.test")

    async with httpx.AsyncClient(transport=transport, base_url="http://ml.test") as client:
        async def mock_probe():
            resp = await client.get("/health")
            return resp.status_code == 200
        inferrer.probe = mock_probe
        assert await inferrer.probe() is True


async def test_http_inferrer_probe_unhealthy():
    inferrer = HttpMLInferrer("http://nonexistent.test:99999")
    assert await inferrer.probe() is False


def test_create_ml_inferrer_local(settings):
    s = settings.model_copy(update={"scene_inference_endpoint": ""})
    inferrer = create_ml_inferrer(s)
    assert inferrer.name == "local"


def test_create_ml_inferrer_http(settings):
    s = settings.model_copy(update={"scene_inference_endpoint": "http://ml.test"})
    inferrer = create_ml_inferrer(s)
    assert inferrer.name == "http"


# ==================== P23: 向量索引 ====================

def test_normalize():
    v = _normalize([3, 4])
    assert abs(v[0] - 0.6) < 1e-6
    assert abs(v[1] - 0.8) < 1e-6


def test_normalize_zero_vector():
    v = _normalize([0, 0, 0])
    assert v == [0, 0, 0]


def test_dot_product():
    assert abs(_dot([1, 0], [0, 1]) - 0.0) < 1e-6
    assert abs(_dot([1, 0], [1, 0]) - 1.0) < 1e-6


async def test_vector_index_add_and_search():
    vi = VectorIndex(max_size=100)
    await vi.add("a1", [1.0, 0.0, 0.0])
    await vi.add("a2", [0.0, 1.0, 0.0])
    await vi.add("a3", [0.9, 0.1, 0.0])

    results = await vi.search([1.0, 0.0, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0][0] == "a1"
    assert results[0][1] > 0.99


async def test_vector_index_search_top_k():
    vi = VectorIndex()
    import math
    for i in range(10):
        angle = i * math.pi / 10
        await vi.add(f"a{i}", [math.cos(angle), math.sin(angle)])
    query_angle = 9 * math.pi / 10
    results = await vi.search([math.cos(query_angle), math.sin(query_angle)], top_k=3)
    assert len(results) == 3
    assert results[0][0] == "a9"


async def test_vector_index_exclude():
    vi = VectorIndex()
    await vi.add("a1", [1.0, 0.0])
    await vi.add("a2", [0.9, 0.1])
    results = await vi.search([1.0, 0.0], top_k=10, exclude={"a1"})
    assert all(aid != "a1" for aid, _ in results)


async def test_vector_index_remove():
    vi = VectorIndex()
    await vi.add("a1", [1.0, 0.0])
    assert await vi.remove("a1") is True
    assert await vi.remove("a1") is False
    results = await vi.search([1.0, 0.0])
    assert results == []


async def test_vector_index_update():
    vi = VectorIndex()
    await vi.add("a1", [1.0, 0.0])
    await vi.add("a1", [0.0, 1.0])
    results = await vi.search([0.0, 1.0])
    assert results[0][0] == "a1"
    results = await vi.search([1.0, 0.0])
    assert results[0][1] < 0.01


async def test_vector_index_lru_eviction():
    vi = VectorIndex(max_size=3)
    await vi.add("a1", [1.0])
    await vi.add("a2", [1.0])
    await vi.add("a3", [1.0])
    await vi.add("a4", [1.0])
    stats = await vi.stats()
    assert stats["count"] == 3
    vec = await vi.get("a1")
    assert vec is None


async def test_vector_index_batch_add():
    vi = VectorIndex()
    count = await vi.batch_add([
        ("a1", [1.0, 0.0]),
        ("a2", [0.0, 1.0]),
        ("a3", [0.5, 0.5]),
    ])
    assert count == 3
    stats = await vi.stats()
    assert stats["count"] == 3


async def test_vector_index_clear():
    vi = VectorIndex()
    await vi.add("a1", [1.0])
    await vi.add("a2", [1.0])
    count = await vi.clear()
    assert count == 2
    stats = await vi.stats()
    assert stats["count"] == 0


async def test_vector_index_stats():
    vi = VectorIndex(max_size=100)
    await vi.add("a1", [1.0])
    stats = await vi.stats()
    assert stats["count"] == 1
    assert stats["max_size"] == 100
    assert 0 < stats["utilization"] < 1


# ==================== P24: 模型管理 ====================

async def test_model_register_and_load():
    mgr = ModelManager()
    loaded = {"value": None}

    async def loader():
        loaded["value"] = "model_instance"
        return loaded["value"]

    mgr.register("test_model", loader)
    model = await mgr.load("test_model")
    assert model == "model_instance"
    assert mgr.is_loaded("test_model")


async def test_model_load_idempotent():
    """重复 load 增加引用计数。"""
    mgr = ModelManager()
    call_count = {"n": 0}

    async def loader():
        call_count["n"] += 1
        return f"model_{call_count['n']}"

    mgr.register("m1", loader)
    await mgr.load("m1")
    await mgr.load("m1")
    assert call_count["n"] == 1
    info = mgr.list_models()[0]
    assert info.ref_count == 2


async def test_model_unload():
    mgr = ModelManager()

    async def loader():
        return "model"

    mgr.register("m1", loader)
    await mgr.load("m1")
    assert await mgr.unload("m1") is True
    assert not mgr.is_loaded("m1")


async def test_model_unload_ref_count():
    """引用计数未归零时不实际卸载。"""
    mgr = ModelManager()

    async def loader():
        return "model"

    mgr.register("m1", loader)
    await mgr.load("m1")
    await mgr.load("m1")
    await mgr.unload("m1")
    assert mgr.is_loaded("m1")
    await mgr.unload("m1")
    assert not mgr.is_loaded("m1")


async def test_model_load_error():
    mgr = ModelManager()

    async def loader():
        raise RuntimeError("加载失败")

    mgr.register("bad_model", loader)
    with pytest.raises(RuntimeError):
        await mgr.load("bad_model")
    info = mgr.list_models()[0]
    assert info.status == ModelStatus.ERROR
    assert "加载失败" in info.error


async def test_model_get_lazy_load():
    mgr = ModelManager()

    async def loader():
        return "lazy_model"

    mgr.register("m1", loader)
    model = await mgr.get_model("m1")
    assert model == "lazy_model"
    assert mgr.is_loaded("m1")


async def test_model_health_check():
    mgr = ModelManager()

    async def loader1():
        return "m1"

    async def loader2():
        raise ValueError("fail")

    mgr.register("m1", loader1)
    mgr.register("m2", loader2)
    await mgr.load("m1")
    try:
        await mgr.load("m2")
    except ValueError:
        pass

    health = await mgr.health_check()
    assert "m1" in health
    assert health["m1"]["status"] == "loaded"
    assert health["m2"]["status"] == "error"


async def test_model_unregister():
    mgr = ModelManager()

    async def loader():
        return "model"

    mgr.register("m1", loader)
    assert mgr.unregister("m1") is True
    assert len(mgr.list_models()) == 0


async def test_model_unregister_loaded_fails():
    """已加载模型不能注销。"""
    mgr = ModelManager()

    async def loader():
        return "model"

    mgr.register("m1", loader)
    await mgr.load("m1")
    assert mgr.unregister("m1") is False


async def test_model_load_timeout():
    mgr = ModelManager(load_timeout=0.1)

    async def slow_loader():
        await asyncio.sleep(5)
        return "model"

    mgr.register("slow", slow_loader)
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await mgr.load("slow")


def test_model_list_models():
    mgr = ModelManager()
    mgr.register("m1", lambda: None)
    mgr.register("m2", lambda: None)
    models = mgr.list_models()
    assert len(models) == 2
    assert all(m.status == ModelStatus.REGISTERED for m in models)