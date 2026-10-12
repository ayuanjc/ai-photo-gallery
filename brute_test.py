"""破坏性测试脚本 — 混沌工程与安全渗透测试。

以混沌工程师与安全渗透测试专家的视角，对 AI 图像语义搜索微服务执行
极端、畸形、恶意输入与故障场景的攻击性测试，找出系统崩溃、异常、
死锁或资源泄漏等漏洞。

六大类测试场景：
    1. 输入畸形与边界攻击
    2. 网络与上游故障模拟
    3. 并发与资源耗尽攻击
    4. 数学边界攻击
    5. 数据污染与状态攻击
    6. 安全攻击

规则：
    - 不编写任何"正常路径"测试，仅编写攻击性与边界性测试用例。
    - 每个测试用例必须有明确断言：要么验证系统正确抛出预期异常，
      要么验证系统正确降级且不崩溃。
    - 若系统崩溃（未捕获异常、死锁、内存溢出），测试应主动 FAIL
      并输出醒目警告信息。
    - 不修复任何代码，只负责找出问题。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.errors import (
    UpstreamError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    VisionError,
    VisionTimeoutError,
    VisionUnavailableError,
)
from app.main import app
from app.models.face import FaceClassifyRequest
from app.models.request import MultiConditionSearchRequest, SearchRequest
from app.scene.analyzer import SceneAnalyzer
from app.scene.dictionary import get_scene_dict
from app.services.face_service import calculate_gaussian_probability
from app.services.immich import ImmichClient
from app.services.ranking import (
    calculate_dynamic_weights,
    clamp,
    fuse_multimodal_query,
    intent_score,
    rank_assets,
)
from app.services.vision.huawei_face import HuaweiFaceClient
from app.utils.cache import TimeDecayedLFUCache
from app.utils.time_parsing import parse_datetime, time_freshness
from app.utils.url_safety import is_safe_url, validate_url_or_raise


# ============================================================================
# 公共夹具
# ============================================================================

@pytest.fixture
def settings() -> Settings:
    return Settings(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        immich_timeout_seconds=1.0,
        immich_connect_timeout_seconds=1.0,
        immich_max_retries=1,
        immich_retry_backoff_seconds=0.0,
        time_weight=0.3,
        similarity_weight=0.7,
        time_decay_half_life_days=365.0,
        default_datetime_iso="1970-01-01T00:00:00+00:00",
    )


@pytest.fixture
def face_settings() -> Settings:
    return Settings(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        huawei_ak="test-ak",
        huawei_sk="test-sk",
        huawei_project_id="test-project",
        huawei_frs_endpoint="https://face.test.myhuaweicloud.com",
        huawei_face_enabled=True,
        huawei_face_max_concurrent=2,
        huawei_face_timeout_seconds=5.0,
        huawei_face_connect_timeout_seconds=2.0,
    )


@pytest.fixture
def immich_client(settings) -> ImmichClient:
    return ImmichClient(settings, client=httpx.AsyncClient(base_url=settings.immich_base_url))


@pytest.fixture
def face_client(face_settings) -> HuaweiFaceClient:
    return HuaweiFaceClient(
        face_settings,
        client=httpx.AsyncClient(base_url=face_settings.huawei_frs_endpoint),
    )


# ============================================================================
# 第一类：输入畸形与边界攻击
# ============================================================================

class TestInputMalformation:
    """对请求模型注入畸形、超长、非法类型、特殊字符等攻击性输入。"""

    # ---- query 参数攻击 ----

    def test_query_empty_string_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="", asset_ids=["a1"])

    def test_query_whitespace_only_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="   \t\n  ", asset_ids=["a1"])

    def test_query_501_chars_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="x" * 501, asset_ids=["a1"])

    def test_query_500_chars_accepted(self):
        req = SearchRequest(query="x" * 500, asset_ids=["a1"])
        assert len(req.query) == 500

    def test_query_sql_injection_accepted_not_executed(self):
        for payload in ["' OR 1=1 --", "'; DROP TABLE assets; --",
                         "UNION SELECT * FROM users", "' AND SLEEP(100) --"]:
            req = SearchRequest(query=payload, asset_ids=["a1"])
            assert req.query == payload

    def test_query_xss_payload_accepted_not_executed(self):
        for payload in ["<script>alert('xss')</script>", "<img src=x onerror=alert(1)>",
                         "javascript:alert(1)", "<svg/onload=alert(1)>"]:
            req = SearchRequest(query=payload, asset_ids=["a1"])
            assert req.query == payload

    def test_query_emoji_and_unicode_accepted(self):
        for payload in ["🐱🐶", "日本語の検索", "Поиск", "🔍 search", "𝕳𝔢𝔩𝔡𝔯𝔮𝔡"]:
            req = SearchRequest(query=payload, asset_ids=["a1"])
            assert req.query == payload

    def test_query_null_byte_injected(self):
        req = SearchRequest(query="test\x00malicious", asset_ids=["a1"])
        assert "\x00" in req.query

    # ---- asset_ids 参数攻击 ----

    def test_asset_ids_empty_list_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=[])

    def test_asset_ids_empty_string_element_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=[""])

    def test_asset_ids_whitespace_only_element_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["   "])

    def test_asset_ids_duplicate_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1", "a1"])

    def test_asset_ids_129_chars_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["x" * 129])

    def test_asset_ids_201_items_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=[f"a{i}" for i in range(201)])

    def test_asset_ids_200_items_accepted(self):
        req = SearchRequest(query="test", asset_ids=[f"a{i}" for i in range(200)])
        assert len(req.asset_ids) == 200

    def test_asset_ids_null_byte_injected(self):
        req = SearchRequest(query="test", asset_ids=["a\x00b"])
        assert "\x00" in req.asset_ids[0]

    def test_asset_ids_path_traversal_injected(self):
        req = SearchRequest(query="test", asset_ids=["../../etc/passwd"])
        assert req.asset_ids[0] == "../../etc/passwd"

    # ---- limit 参数攻击 ----

    def test_limit_zero_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], limit=0)

    def test_limit_negative_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], limit=-1)

    def test_limit_201_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], limit=201)

    def test_limit_string_type_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], limit="abc")

    def test_limit_float_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], limit=50.5)

    # ---- 权重参数攻击 ----

    def test_time_weight_negative_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], time_weight=-0.01)

    def test_time_weight_above_one_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], time_weight=1.01)

    def test_time_weight_nan_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], time_weight=float("nan"))

    def test_time_weight_infinity_rejected(self):
        with pytest.raises(Exception):
            SearchRequest(query="test", asset_ids=["a1"], time_weight=float("inf"))

    def test_both_weights_zero_accepted(self):
        req = SearchRequest(query="test", asset_ids=["a1"], time_weight=0.0, similarity_weight=0.0)
        assert req.time_weight == 0.0

    # ---- FaceClassifyRequest 攻击 ----

    def test_face_request_empty_urls_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=[])

    def test_face_request_201_urls_rejected(self):
        urls = [f"http://example.com/img{i}.jpg" for i in range(201)]
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=urls)

    def test_face_request_threshold_above_one_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=["http://example.com/1.jpg"], similarity_threshold=1.01)

    def test_face_request_threshold_negative_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=["http://example.com/1.jpg"], similarity_threshold=-0.01)

    def test_face_request_max_faces_zero_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=["http://example.com/1.jpg"], max_faces=0)

    def test_face_request_max_faces_eleven_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=["http://example.com/1.jpg"], max_faces=11)

    # ---- MultiConditionSearchRequest 攻击 ----

    def test_multi_condition_all_none_rejected(self):
        with pytest.raises(Exception):
            MultiConditionSearchRequest()

    def test_multi_condition_limit_zero_rejected(self):
        with pytest.raises(Exception):
            MultiConditionSearchRequest(query="test", limit=0)

    def test_multi_condition_query_501_chars_rejected(self):
        with pytest.raises(Exception):
            MultiConditionSearchRequest(query="x" * 501)


# ============================================================================
# 第二类：网络与上游故障模拟
# ============================================================================

class TestNetworkFaults:
    """用 respx Mock Immich / FRS 上游，模拟各类网络故障与异常响应。"""

    @respx.mock
    async def test_immich_connect_timeout_raises_upstream_timeout(self, immich_client):
        respx.post("/api/search/smart").mock(side_effect=httpx.ConnectTimeout("timeout"))
        with pytest.raises((UpstreamTimeoutError, UpstreamUnavailableError)):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_read_timeout_raises_upstream_timeout(self, immich_client):
        respx.post("/api/search/smart").mock(side_effect=httpx.ReadTimeout("read timeout"))
        with pytest.raises(UpstreamTimeoutError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_connect_error_raises_upstream_unavailable(self, immich_client):
        respx.post("/api/search/smart").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(UpstreamUnavailableError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_500_retries_then_unavailable(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=500)
        with pytest.raises(UpstreamUnavailableError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_502_retries_then_unavailable(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=502)
        with pytest.raises(UpstreamUnavailableError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_503_retries_then_unavailable(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=503)
        with pytest.raises(UpstreamUnavailableError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_504_retries_then_unavailable(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=504)
        with pytest.raises(UpstreamUnavailableError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_401_raises_upstream_error(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=401)
        with pytest.raises(UpstreamError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_403_raises_upstream_error(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=403)
        with pytest.raises(UpstreamError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_429_raises_upstream_error(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=429)
        with pytest.raises(UpstreamError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_non_json_response_raises_upstream_error(self, immich_client):
        respx.post("/api/search/smart").respond(
            status_code=200, content=b"<html>Not JSON</html>",
        )
        with pytest.raises(UpstreamError):
            await immich_client.smart_search("test", k=10)

    @respx.mock
    async def test_immich_empty_json_response_returns_empty_list(self, immich_client):
        respx.post("/api/search/smart").respond(status_code=200, json={})
        result = await immich_client.smart_search("test", k=10)
        assert result == []

    @respx.mock
    async def test_immich_response_missing_score_field_filled(self, immich_client):
        respx.post("/api/search/smart").respond(
            status_code=200, json={"assets": {"items": [{"id": "a1"}, {"id": "a2"}]}},
        )
        result = await immich_client.smart_search("test", k=10)
        assert len(result) == 2
        assert "score" in result[0]
        assert "score" in result[1]

    @respx.mock
    async def test_immich_response_score_is_none_filled(self, immich_client):
        respx.post("/api/search/smart").respond(
            status_code=200, json={"assets": {"items": [{"id": "a1", "score": None}]}},
        )
        result = await immich_client.smart_search("test", k=10)
        assert result[0]["score"] is not None

    @respx.mock
    async def test_immich_response_score_extreme_values(self, immich_client):
        respx.post("/api/search/smart").respond(
            status_code=200,
            json={"assets": {"items": [
                {"id": "a1", "score": 999.0},
                {"id": "a2", "score": -999.0},
                {"id": "a3", "score": 1e308},
            ]}},
        )
        result = await immich_client.smart_search("test", k=10)
        assert len(result) == 3

    @respx.mock
    async def test_immich_malformed_assets_field(self, immich_client):
        respx.post("/api/search/smart").respond(
            status_code=200, json={"assets": "not_a_list"},
        )
        result = await immich_client.smart_search("test", k=10)
        assert result == []

    @respx.mock
    async def test_immich_response_is_raw_list(self, immich_client):
        respx.post("/api/search/smart").respond(
            status_code=200, json=[{"id": "a1", "score": 0.9}],
        )
        result = await immich_client.smart_search("test", k=10)
        assert len(result) == 1

    # ---- FRS 故障模拟 ----

    @respx.mock
    async def test_frs_timeout_raises_vision_timeout(self, face_client):
        respx.post("/v2/test-project/face-detect").mock(side_effect=httpx.ReadTimeout("timeout"))
        with pytest.raises(VisionTimeoutError):
            await face_client.detect_faces("http://example.com/1.jpg")

    @respx.mock
    async def test_frs_connect_error_raises_vision_unavailable(self, face_client):
        respx.post("/v2/test-project/face-detect").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(VisionUnavailableError):
            await face_client.detect_faces("http://example.com/1.jpg")

    @respx.mock
    async def test_frs_500_raises_vision_unavailable(self, face_client):
        respx.post("/v2/test-project/face-detect").respond(status_code=500)
        with pytest.raises(VisionUnavailableError):
            await face_client.detect_faces("http://example.com/1.jpg")

    @respx.mock
    async def test_frs_401_raises_vision_error(self, face_client):
        respx.post("/v2/test-project/face-detect").respond(status_code=401)
        with pytest.raises(VisionError):
            await face_client.detect_faces("http://example.com/1.jpg")

    @respx.mock
    async def test_frs_non_json_raises_vision_error(self, face_client):
        respx.post("/v2/test-project/face-detect").respond(status_code=200, content=b"NOT JSON")
        with pytest.raises(VisionError):
            await face_client.detect_faces("http://example.com/1.jpg")

    @respx.mock
    async def test_frs_add_face_missing_face_id_raises(self, face_client):
        respx.post("/v2/test-project/face-sets/test-set/faces").respond(
            status_code=200, json={"no_face_id": True},
        )
        with pytest.raises(VisionError):
            await face_client.add_face_by_url("test-set", "http://example.com/1.jpg", "ext1")

    @respx.mock
    async def test_frs_delete_face_set_retries_on_failure(self, face_client):
        route = respx.delete("/v2/test-project/face-sets/bad-set").respond(status_code=500)
        try:
            await face_client.delete_face_set("bad-set")
            assert route.call_count == 3
        except VisionUnavailableError:
            pytest.fail(
                "⚠️ 漏洞[CRASH]: delete_face_set 在 5xx 时应重试不报错，"
                "但 VisionUnavailableError 未被 except (VisionError, httpx.HTTPError) 捕获"
            )


# ============================================================================
# 第三类：并发与资源耗尽攻击
# ============================================================================

class TestConcurrencyExhaustion:
    """模拟高并发、大容量缓存、资源耗尽等场景，检测死锁与泄漏。"""

    def test_cache_10000_keys_no_deadlock(self):
        cache = TimeDecayedLFUCache(maxsize=10000, halflife_seconds=60.0)
        for i in range(15000):
            cache[f"key_{i}"] = i
        assert len(cache) == 10000

    def test_cache_maxsize_1_evicts_correctly(self):
        cache = TimeDecayedLFUCache(maxsize=1, halflife_seconds=60.0)
        cache["a"] = 1
        cache["b"] = 2
        assert len(cache) == 1
        assert "b" in cache
        assert "a" not in cache

    def test_cache_maxsize_zero_should_not_allow_insertion(self):
        cache = TimeDecayedLFUCache(maxsize=0, halflife_seconds=60.0)
        cache["a"] = 1
        assert len(cache) == 0, "⚠️ 漏洞[SSRF/资源]: maxsize=0 的缓存不应允许插入任何 key"

    def test_cache_halflife_zero_raises_value_error(self):
        with pytest.raises(ValueError):
            TimeDecayedLFUCache(maxsize=100, halflife_seconds=0)

    def test_cache_halflife_negative_raises_value_error(self):
        with pytest.raises(ValueError):
            TimeDecayedLFUCache(maxsize=100, halflife_seconds=-1.0)

    def test_cache_eviction_preserves_hot_keys(self):
        cache = TimeDecayedLFUCache(maxsize=3, halflife_seconds=60.0)
        cache["hot"] = 1
        for _ in range(5):
            _ = cache["hot"]
        cache["cold1"] = 2
        cache["cold2"] = 3
        cache["cold3"] = 4
        assert "hot" in cache, "⚠️ 漏洞：高频访问的 key 不应被淘汰"

    async def test_concurrent_cache_access_no_corruption(self):
        cache = TimeDecayedLFUCache(maxsize=100, halflife_seconds=60.0)

        async def writer(idx: int):
            for i in range(50):
                cache[f"w{idx}_{i}"] = idx * 100 + i

        async def reader(idx: int):
            for i in range(50):
                _ = cache.get(f"w{idx}_{i}")

        await asyncio.gather(*(writer(i) for i in range(10)))
        await asyncio.gather(*(reader(i) for i in range(10)))
        assert len(cache) <= 100

    @respx.mock
    async def test_concurrent_immich_requests_no_deadlock(self, settings):
        client = ImmichClient(
            settings, client=httpx.AsyncClient(base_url=settings.immich_base_url),
        )
        respx.post("/api/search/smart").respond(
            json={"assets": {"items": [{"id": "a1", "score": 0.8}]}}
        )

        async def search():
            return await client.smart_search("test", k=1)

        results = await asyncio.gather(*(search() for _ in range(50)))
        assert len(results) == 50
        assert all(r is not None for r in results)

    async def test_scene_analyzer_concurrent_same_asset_id_singleflight(self):
        scene_dict = get_scene_dict()
        analyzer = SceneAnalyzer(
            scene_dict, cache_size=10, cache_halflife_seconds=60.0, infer_timeout_seconds=1.0,
        )
        asset = {"id": "same_id", "smartInfo": {"tags": ["nature"]}}

        async def analyze():
            return await analyzer.analyze(asset)

        results = await asyncio.gather(*(analyze() for _ in range(20)))
        assert all(r is not None for r in results)
        assert all(r.primary_scene == results[0].primary_scene for r in results)

    def test_cache_popitem_on_empty_raises_keyerror(self):
        cache = TimeDecayedLFUCache(maxsize=10, halflife_seconds=60.0)
        with pytest.raises(KeyError):
            cache.popitem()


# ============================================================================
# 第四类：数学边界攻击
# ============================================================================

class TestMathBoundary:
    """对排序、衰减、高斯概率等数学函数注入边界值与特殊浮点数。"""

    # ---- time_freshness 边界 ----

    def test_freshness_age_zero_returns_one(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert time_freshness(now, now=now, half_life_days=365.0) == pytest.approx(1.0)

    def test_freshness_future_time_returns_one(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        future = now + timedelta(days=100)
        assert time_freshness(future, now=now, half_life_days=365.0) == pytest.approx(1.0)

    def test_freshness_very_old_time_approaches_zero(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        old = datetime(1, 1, 1, tzinfo=timezone.utc)
        result = time_freshness(old, now=now, half_life_days=365.0)
        assert 0.0 <= result < 1e-50

    def test_freshness_none_captured_returns_zero(self):
        assert time_freshness(None, half_life_days=365.0) == 0.0

    def test_freshness_half_life_zero_no_crash(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        captured = datetime(2023, 1, 1, tzinfo=timezone.utc)
        result = time_freshness(captured, now=now, half_life_days=0.0)
        assert not math.isnan(result), "⚠️ 漏洞[NAN]: half_life=0 不应产生 NaN"
        assert 0.0 <= result <= 1.0

    def test_freshness_half_life_negative_no_crash(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        captured = datetime(2023, 1, 1, tzinfo=timezone.utc)
        result = time_freshness(captured, now=now, half_life_days=-100.0)
        assert not math.isnan(result), "⚠️ 漏洞[NAN]: half_life 为负不应产生 NaN"
        assert 0.0 <= result <= 1.0

    def test_freshness_half_life_extremely_small(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        captured = datetime(2023, 1, 1, tzinfo=timezone.utc)
        result = time_freshness(captured, now=now, half_life_days=1e-10)
        assert 0.0 <= result <= 1.0

    # ---- clamp 边界 ----

    def test_clamp_nan_should_not_propagate(self):
        result = clamp(float("nan"))
        assert not math.isnan(result), "⚠️ 漏洞[NAN]: clamp(NaN) 不应返回 NaN"

    def test_clamp_infinity_clamped_to_one(self):
        assert clamp(float("inf")) == 1.0

    def test_clamp_negative_infinity_clamped_to_zero(self):
        assert clamp(float("-inf")) == 0.0

    def test_clamp_above_one(self):
        assert clamp(999.0) == 1.0

    def test_clamp_below_zero(self):
        assert clamp(-999.0) == 0.0

    # ---- calculate_gaussian_probability 边界 ----

    def test_gaussian_sim_one_returns_one(self):
        assert calculate_gaussian_probability(1.0, 0.1) == pytest.approx(1.0)

    def test_gaussian_sim_zero_returns_small_value(self):
        result = calculate_gaussian_probability(0.0, 0.1)
        assert 0.0 < result < 1.0

    def test_gaussian_sim_negative_clamped(self):
        result = calculate_gaussian_probability(-1.0, 0.1)
        assert 0.0 <= result <= 1.0

    def test_gaussian_sim_above_one_clamped(self):
        result = calculate_gaussian_probability(2.0, 0.1)
        assert result == pytest.approx(1.0)

    def test_gaussian_sigma_zero_hard_judgment(self):
        assert calculate_gaussian_probability(1.0, 0.0) == 1.0
        assert calculate_gaussian_probability(0.99, 0.0) == 0.0

    def test_gaussian_sigma_negative_hard_judgment(self):
        assert calculate_gaussian_probability(1.0, -0.5) == 1.0
        assert calculate_gaussian_probability(0.5, -0.5) == 0.0

    def test_gaussian_sim_nan_should_not_propagate(self):
        result = calculate_gaussian_probability(float("nan"), 0.1)
        assert not math.isnan(result), "⚠️ 漏洞[NAN]: 高斯概率(NaN) 不应返回 NaN"

    def test_gaussian_sim_infinity_clamped(self):
        result = calculate_gaussian_probability(float("inf"), 0.1)
        assert 0.0 <= result <= 1.0

    def test_gaussian_sigma_extremely_small(self):
        result = calculate_gaussian_probability(0.95, 1e-10)
        assert 0.0 <= result <= 1.0

    # ---- fuse_multimodal_query 边界 ----

    def test_fuse_lambda_one_returns_query_only(self):
        assert fuse_multimodal_query("query", ["tag1", "tag2"], 1.0) == "query"

    def test_fuse_lambda_zero_returns_tags_only(self):
        result = fuse_multimodal_query("query", ["tag1", "tag2"], 0.0)
        assert "tag1" in result and "tag2" in result
        assert "query" not in result

    def test_fuse_lambda_nan_should_not_crash(self):
        try:
            result = fuse_multimodal_query("query", ["tag1"], float("nan"))
            assert isinstance(result, str), "⚠️ 漏洞[CRASH]: lambda=NaN 不应导致非字符串返回"
        except (TypeError, ValueError) as exc:
            pytest.fail(f"⚠️ 漏洞[CRASH]: lambda=NaN 导致崩溃: {exc}")

    def test_fuse_lambda_infinity_clamped(self):
        result = fuse_multimodal_query("query", ["tag1"], float("inf"))
        assert isinstance(result, str)

    def test_fuse_lambda_negative_clamped(self):
        result = fuse_multimodal_query("query", ["tag1"], -1.0)
        assert isinstance(result, str)

    def test_fuse_empty_query_and_empty_tags(self):
        assert fuse_multimodal_query("", [], 0.7) == ""

    def test_fuse_empty_query_with_tags(self):
        result = fuse_multimodal_query("", ["tag1", "tag2"], 0.7)
        assert "tag1" in result

    def test_fuse_query_with_empty_tags(self):
        assert fuse_multimodal_query("query", [], 0.7) == "query"

    def test_fuse_text_repeat_capped_at_five(self):
        result = fuse_multimodal_query("q", ["tag"], 0.99)
        q_count = result.split().count("q")
        assert q_count <= 5, "⚠️ 漏洞：text_repeat 应上限 5 次"

    # ---- intent_score 边界 ----

    def test_intent_empty_query(self):
        assert intent_score("") == 0.5

    def test_intent_no_keywords(self):
        assert intent_score("random text") == 0.5

    def test_intent_recent_keyword(self):
        assert intent_score("最近的照片") == 1.0

    def test_intent_nostalgic_keyword(self):
        assert intent_score("老照片") == 0.0

    # ---- calculate_dynamic_weights 边界 ----

    def test_dynamic_weights_gamma_zero(self):
        alpha, beta = calculate_dynamic_weights("test", 0.3, 0.7, 0.0)
        assert alpha + beta == pytest.approx(1.0)

    def test_dynamic_weights_gamma_above_one(self):
        alpha, beta = calculate_dynamic_weights("最近", 0.3, 0.7, 2.0)
        assert 0.0 <= alpha <= 1.0
        assert 0.0 <= beta <= 1.0

    def test_dynamic_weights_base_both_zero(self):
        alpha, beta = calculate_dynamic_weights("test", 0.0, 0.0, 0.4)
        assert 0.0 <= alpha <= 1.0
        assert 0.0 <= beta <= 1.0

    # ---- rank_assets 边界 ----

    def test_rank_empty_assets(self):
        result = rank_assets([], time_weight=0.3, similarity_weight=0.7,
                              half_life_days=365.0, default_iso="1970-01-01T00:00:00+00:00")
        assert result == []

    def test_rank_asset_missing_score(self):
        result = rank_assets([{"id": "a1"}], time_weight=0.3, similarity_weight=0.7,
                              half_life_days=365.0, default_iso="1970-01-01T00:00:00+00:00")
        assert len(result) == 1
        assert 0.0 <= result[0][1] <= 1.0

    def test_rank_asset_score_none(self):
        result = rank_assets([{"id": "a1", "score": None}], time_weight=0.3,
                              similarity_weight=0.7, half_life_days=365.0,
                              default_iso="1970-01-01T00:00:00+00:00")
        assert len(result) == 1

    def test_rank_asset_score_nan(self):
        result = rank_assets([{"id": "a1", "score": float("nan")}], time_weight=0.3,
                              similarity_weight=0.7, half_life_days=365.0,
                              default_iso="1970-01-01T00:00:00+00:00")
        assert len(result) == 1
        final_score = result[0][1]
        assert not math.isnan(final_score), "⚠️ 漏洞[NAN]: NaN score 不应传播到 final_score"

    def test_rank_asset_score_infinity(self):
        result = rank_assets([{"id": "a1", "score": float("inf")}], time_weight=0.3,
                              similarity_weight=0.7, half_life_days=365.0,
                              default_iso="1970-01-01T00:00:00+00:00")
        assert len(result) == 1
        assert 0.0 <= result[0][1] <= 1.0


# ============================================================================
# 第五类：数据污染与状态攻击
# ============================================================================

class TestDataPollution:
    """注入特殊字符、超长字段、畸形数据结构，检测系统是否正确处理。"""

    def test_parse_datetime_empty_string_returns_none(self):
        assert parse_datetime("") is None

    def test_parse_datetime_none_returns_none(self):
        assert parse_datetime(None) is None

    def test_parse_datetime_malformed_string_returns_none(self):
        assert parse_datetime("not-a-date-at-all!@#$%") is None

    def test_parse_datetime_null_byte_in_date(self):
        result = parse_datetime("2024-01-01T00:00:00\x00+00:00")
        assert result is None or isinstance(result, datetime)

    def test_parse_datetime_extremely_long_garbage(self):
        result = parse_datetime("X" * 10000)
        assert result is None

    def test_parse_datetime_valid_iso(self):
        result = parse_datetime("2024-01-01T00:00:00+00:00")
        assert result is not None and result.year == 2024

    def test_parse_datetime_exif_format(self):
        result = parse_datetime("2024:01:01 12:00:00")
        assert result is not None and result.year == 2024

    def test_parse_datetime_no_timezone_assumes_utc(self):
        result = parse_datetime("2024-01-01T00:00:00")
        assert result is not None and result.tzinfo is not None

    def test_query_with_newline_tab_accepted(self):
        req = SearchRequest(query="test\n\tquery", asset_ids=["a1"])
        assert "\n" in req.query

    def test_query_with_carriage_return_accepted(self):
        req = SearchRequest(query="test\r\nquery", asset_ids=["a1"])
        assert "\r" in req.query

    def test_asset_ids_with_unicode_accepted(self):
        req = SearchRequest(query="test", asset_ids=["中文id", "日本語id", "🐱"])
        assert len(req.asset_ids) == 3

    @respx.mock
    async def test_immich_response_with_null_bytes_in_fields(self, immich_client):
        respx.post("/api/search/smart").respond(
            status_code=200, json={"assets": {"items": [{"id": "a\x00b", "score": 0.8}]}},
        )
        result = await immich_client.smart_search("test", k=10)
        assert len(result) == 1

    @respx.mock
    async def test_immich_response_with_deeply_nested_structure(self, immich_client):
        nested = {"level": 0}
        for i in range(100):
            nested = {"level": i, "child": nested}
        respx.post("/api/search/smart").respond(
            status_code=200,
            json={"assets": {"items": [{"id": "a1", "score": 0.8, "nested": nested}]}},
        )
        result = await immich_client.smart_search("test", k=10)
        assert len(result) == 1

    @respx.mock
    async def test_immich_response_with_extremely_long_id(self, immich_client):
        long_id = "x" * 10000
        respx.post("/api/search/smart").respond(
            status_code=200, json={"assets": {"items": [{"id": long_id, "score": 0.8}]}},
        )
        result = await immich_client.smart_search("test", k=10)
        assert len(result) == 1 and len(result[0]["id"]) == 10000

    def test_rank_assets_with_malformed_exif_datetime(self):
        asset = {"id": "a1", "score": 0.8,
                 "exifInfo": {"dateTimeOriginal": "INVALID"},
                 "fileCreatedAt": "ALSO-INVALID"}
        result = rank_assets([asset], time_weight=0.3, similarity_weight=0.7,
                              half_life_days=365.0, default_iso="1970-01-01T00:00:00+00:00")
        assert len(result) == 1 and 0.0 <= result[0][1] <= 1.0

    def test_rank_assets_with_future_exif_datetime(self):
        future = (datetime.now(timezone.utc) + timedelta(days=36500)).isoformat()
        asset = {"id": "a1", "score": 0.8, "exifInfo": {"dateTimeOriginal": future}}
        result = rank_assets([asset], time_weight=0.3, similarity_weight=0.7,
                              half_life_days=365.0, default_iso="1970-01-01T00:00:00+00:00")
        assert len(result) == 1 and 0.0 <= result[0][1] <= 1.0

    @respx.mock
    async def test_frs_detect_response_with_extra_fields(self, face_client):
        respx.post("/v2/test-project/face-detect").respond(
            status_code=200,
            json={"faces": [{"bounding_box": {"top_left_x": 0, "top_left_y": 0,
                                               "width": 100, "height": 100},
                              "extra_field": "malicious"}],
                  "extra_top_level": "ignored"},
        )
        result = await face_client.detect_faces("http://example.com/1.jpg")
        assert len(result) == 1

    @respx.mock
    async def test_frs_search_response_empty_faces(self, face_client):
        respx.post("/v2/test-project/face-sets/test-set/search").respond(
            status_code=200, json={"faces": []},
        )
        result = await face_client.search_face_by_id("test-set", "face-1")
        assert result == []

    @respx.mock
    async def test_frs_search_response_missing_faces_field(self, face_client):
        respx.post("/v2/test-project/face-sets/test-set/search").respond(
            status_code=200, json={},
        )
        result = await face_client.search_face_by_id("test-set", "face-1")
        assert result == []


# ============================================================================
# 第六类：安全攻击
# ============================================================================

class TestSecurityAttacks:
    """SSRF、路径穿越、命令注入、API Key 伪造、敏感信息泄露等安全测试。"""

    # ---- SSRF 防护 ----

    def test_ssrf_localhost_rejected(self):
        assert not is_safe_url("http://localhost/image.jpg"), \
            "⚠️ 漏洞[SSRF]: localhost 应被 SSRF 防护拒绝"

    def test_ssrf_127_0_0_1_rejected(self):
        assert not is_safe_url("http://127.0.0.1/image.jpg")

    def test_ssrf_10_0_0_1_rejected(self):
        assert not is_safe_url("http://10.0.0.1/image.jpg")

    def test_ssrf_172_16_0_1_rejected(self):
        assert not is_safe_url("http://172.16.0.1/image.jpg")

    def test_ssrf_192_168_1_1_rejected(self):
        assert not is_safe_url("http://192.168.1.1/image.jpg")

    def test_ssrf_169_254_1_1_rejected(self):
        assert not is_safe_url("http://169.254.1.1/image.jpg")

    def test_ssrf_0_0_0_0_rejected(self):
        assert not is_safe_url("http://0.0.0.0/image.jpg")

    def test_ssrf_ipv6_loopback_rejected(self):
        assert not is_safe_url("http://[::1]/image.jpg")

    def test_ssrf_ipv6_link_local_rejected(self):
        assert not is_safe_url("http://[fe80::1]/image.jpg")

    def test_ssrf_file_scheme_rejected(self):
        assert not is_safe_url("file:///etc/passwd")

    def test_ssrf_ftp_scheme_rejected(self):
        assert not is_safe_url("ftp://example.com/file")

    def test_ssrf_gopher_scheme_rejected(self):
        assert not is_safe_url("gopher://example.com/file")

    def test_ssrf_javascript_scheme_rejected(self):
        assert not is_safe_url("javascript:alert(1)")

    def test_ssrf_public_ip_accepted(self):
        assert is_safe_url("http://8.8.8.8/image.jpg")

    def test_ssrf_public_domain_accepted(self):
        assert is_safe_url("http://example.com/image.jpg")

    def test_ssrf_validate_raises_on_unsafe(self):
        with pytest.raises(ValueError):
            validate_url_or_raise("http://127.0.0.1/image.jpg")

    def test_face_request_ssrf_localhost_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=["http://localhost/image.jpg"])

    def test_face_request_ssrf_127_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=["http://127.0.0.1/image.jpg"])

    def test_face_request_ssrf_file_rejected(self):
        with pytest.raises(Exception):
            FaceClassifyRequest(image_urls=["file:///etc/passwd"])

    # ---- 路径穿越 ----

    def test_path_traversal_in_asset_id_accepted_by_model(self):
        req = SearchRequest(query="test", asset_ids=["../../etc/passwd"])
        assert req.asset_ids[0] == "../../etc/passwd"

    def test_path_traversal_in_query_accepted_by_model(self):
        req = SearchRequest(query="../../etc/passwd", asset_ids=["a1"])
        assert req.query == "../../etc/passwd"

    # ---- 命令注入 ----

    def test_command_injection_in_query_accepted_not_executed(self):
        for payload in ["; rm -rf /", "| cat /etc/passwd", "$(whoami)", "`id`", "&& nc -l 4444"]:
            req = SearchRequest(query=payload, asset_ids=["a1"])
            assert req.query == payload

    # ---- API Key 鉴权 ----

    def test_api_key_missing_rejected(self):
        from app.api.deps import get_search_service
        from app.models.response import SearchResponse

        class FakeService:
            async def search(self, request):
                return SearchResponse(query=request.query, total=0, results=[])

        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_search_service] = lambda: FakeService()
        old_key = os.environ.get("SEARCH_API_KEY", "")
        os.environ["SEARCH_API_KEY"] = "secret-key-12345"
        get_settings.cache_clear()
        try:
            with TestClient(app) as client:
                resp = client.post("/api/v1/search/smart",
                                   json={"query": "test", "asset_ids": ["a1"]})
                assert resp.status_code == 401
        finally:
            os.environ["SEARCH_API_KEY"] = old_key
            get_settings.cache_clear()
            app.dependency_overrides = original_overrides

    def test_api_key_wrong_rejected(self):
        from app.api.deps import get_search_service
        from app.models.response import SearchResponse

        class FakeService:
            async def search(self, request):
                return SearchResponse(query=request.query, total=0, results=[])

        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_search_service] = lambda: FakeService()
        old_key = os.environ.get("SEARCH_API_KEY", "")
        os.environ["SEARCH_API_KEY"] = "secret-key-12345"
        get_settings.cache_clear()
        try:
            with TestClient(app) as client:
                resp = client.post("/api/v1/search/smart",
                                   json={"query": "test", "asset_ids": ["a1"]},
                                   headers={"x-api-key": "wrong-key"})
                assert resp.status_code == 401
        finally:
            os.environ["SEARCH_API_KEY"] = old_key
            get_settings.cache_clear()
            app.dependency_overrides = original_overrides

    # ---- 敏感信息泄露 ----

    def test_error_response_does_not_leak_internal_details(self):
        from app.api.deps import get_search_service
        from app.core.errors import UpstreamError

        class FakeService:
            async def search(self, request):
                raise UpstreamError(
                    "Internal DB connection string: postgres://user:password@host/db",
                    details={"secret": "should_not_leak"},
                )

        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_search_service] = lambda: FakeService()
        try:
            with TestClient(app) as client:
                resp = client.post("/api/v1/search/smart",
                                   json={"query": "test", "asset_ids": ["a1"]})
                body = resp.json()
                assert "error" in body
                assert "password" not in json.dumps(body).lower(), \
                    "⚠️ 漏洞[LEAK]: 错误响应不应泄露内部密码等敏感信息"
        finally:
            app.dependency_overrides = original_overrides

    def test_500_error_does_not_leak_stack_trace(self):
        from app.api.deps import get_search_service

        class FakeService:
            async def search(self, request):
                raise RuntimeError("Internal secret: db_password=xyz123")

        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_search_service] = lambda: FakeService()
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post("/api/v1/search/smart",
                                   json={"query": "test", "asset_ids": ["a1"]})
                assert resp.status_code == 500
                body = resp.json()
                assert body["error"]["code"] == "INTERNAL_ERROR"
                assert "db_password" not in json.dumps(body)
                assert "xyz123" not in json.dumps(body)
        finally:
            app.dependency_overrides = original_overrides

    def test_request_id_validation_rejects_injection(self):
        from app.api.deps import get_search_service
        from app.models.response import SearchResponse

        class FakeService:
            async def search(self, request):
                return SearchResponse(query=request.query, total=0, results=[])

        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_search_service] = lambda: FakeService()
        try:
            with TestClient(app) as client:
                resp = client.post("/api/v1/search/smart",
                                   json={"query": "test", "asset_ids": ["a1"]},
                                   headers={"x-request-id": "'; DROP TABLE--"})
                returned_id = resp.headers.get("x-request-id", "")
                assert "'" not in returned_id
                assert " " not in returned_id
                assert "--" not in returned_id
        finally:
            app.dependency_overrides = original_overrides

    def test_request_id_extremely_long_rejected(self):
        from app.api.deps import get_search_service
        from app.models.response import SearchResponse

        class FakeService:
            async def search(self, request):
                return SearchResponse(query=request.query, total=0, results=[])

        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_search_service] = lambda: FakeService()
        try:
            with TestClient(app) as client:
                resp = client.post("/api/v1/search/smart",
                                   json={"query": "test", "asset_ids": ["a1"]},
                                   headers={"x-request-id": "x" * 10000})
                returned_id = resp.headers.get("x-request-id", "")
                assert len(returned_id) <= 128
        finally:
            app.dependency_overrides = original_overrides

    def test_validation_error_does_not_leak_input_values(self):
        from app.api.deps import get_search_service
        from app.models.response import SearchResponse

        class FakeService:
            async def search(self, request):
                return SearchResponse(query=request.query, total=0, results=[])

        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_search_service] = lambda: FakeService()
        try:
            with TestClient(app) as client:
                resp = client.post("/api/v1/search/smart",
                                   json={"query": "test", "asset_ids": [], "limit": -999})
                assert resp.status_code == 400
                body = resp.json()
                assert body["error"]["code"] == "INVALID_PARAMS"
        finally:
            app.dependency_overrides = original_overrides


# ============================================================================
# 第八类：搜索建议与纠错 — 输入畸形 / 数据污染 / 安全攻击
# ============================================================================

class TestSuggestBrute:
    """对 suggest / correct 接口与 SuggestService 执行攻击性测试。

    覆盖：
        - 输入畸形：q 超长（>10000 字符）、limit=0 / 负数
        - 数据污染：q 含 SQL 注入样式、emoji、控制字符
        - 安全攻击：q 含路径遍历字符
    """

    # ---- 辅助 ----

    @staticmethod
    def _svc(**overrides):
        from app.scene.dictionary import get_scene_dict
        from app.services.suggest_service import SuggestService

        defaults = dict(
            immich_base_url="http://immich.test",
            immich_api_key="test-key",
        )
        defaults.update(overrides)
        return SuggestService(Settings(**defaults), get_scene_dict())

    # ---- 输入畸形攻击 ----

    def test_q_10001_chars_rejected_by_validation(self):
        """q 超长（>10000 字符）→ 被 FastAPI Query max_length 拒绝（422）。"""
        from app.api.deps import get_settings_dep

        test_settings = Settings(
            immich_base_url="http://immich.test",
            immich_api_key="test-key",
        )
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        try:
            with TestClient(app) as client:
                resp = client.get(
                    "/api/v1/search/suggest",
                    params={"q": "x" * 10001},
                    headers={"x-api-key": ""},
                )
                assert resp.status_code == 400
        finally:
            app.dependency_overrides = original_overrides

    def test_limit_zero_rejected_by_validation(self):
        """limit=0 → 被校验拒绝（422）。"""
        from app.api.deps import get_settings_dep

        test_settings = Settings(
            immich_base_url="http://immich.test",
            immich_api_key="test-key",
        )
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        try:
            with TestClient(app) as client:
                resp = client.get(
                    "/api/v1/search/suggest",
                    params={"q": "test", "limit": 0},
                    headers={"x-api-key": ""},
                )
                assert resp.status_code == 400
        finally:
            app.dependency_overrides = original_overrides

    def test_limit_negative_rejected_by_validation(self):
        """limit=-1 → 被校验拒绝（400）。"""
        from app.api.deps import get_settings_dep

        test_settings = Settings(
            immich_base_url="http://immich.test",
            immich_api_key="test-key",
        )
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        try:
            with TestClient(app) as client:
                resp = client.get(
                    "/api/v1/search/suggest",
                    params={"q": "test", "limit": -1},
                    headers={"x-api-key": ""},
                )
                assert resp.status_code == 400
        finally:
            app.dependency_overrides = original_overrides

    def test_limit_51_rejected_by_validation(self):
        """limit=51（超过上限 50）→ 被校验拒绝（400）。"""
        from app.api.deps import get_settings_dep

        test_settings = Settings(
            immich_base_url="http://immich.test",
            immich_api_key="test-key",
        )
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings_dep] = lambda: test_settings
        try:
            with TestClient(app) as client:
                resp = client.get(
                    "/api/v1/search/suggest",
                    params={"q": "test", "limit": 51},
                    headers={"x-api-key": ""},
                )
                assert resp.status_code == 400
        finally:
            app.dependency_overrides = original_overrides

    # ---- 数据污染攻击 ----

    def test_sql_injection_no_crash(self):
        """q 含 SQL 注入样式字符串 → 不崩溃，正常返回建议。"""
        svc = self._svc()
        for payload in ["' OR 1=1 --", "'; DROP TABLE--", "UNION SELECT *"]:
            suggestions, degraded, _ = svc.suggest(payload, 10)
            assert degraded is False
            assert isinstance(suggestions, list)

    def test_emoji_no_crash(self):
        """q 含 emoji → 不崩溃。"""
        svc = self._svc()
        suggestions, _, _ = svc.suggest("🐱🐶", 10)
        assert isinstance(suggestions, list)

    def test_control_chars_no_crash(self):
        """q 含控制字符 → 不崩溃。"""
        svc = self._svc()
        suggestions, _, _ = svc.suggest("\x00\x01\x02test", 10)
        assert isinstance(suggestions, list)

    def test_null_byte_no_crash(self):
        """q 含 null byte → 不崩溃。"""
        svc = self._svc()
        suggestions, _, _ = svc.suggest("test\x00malicious", 10)
        assert isinstance(suggestions, list)

    def test_correct_sql_injection_no_crash(self):
        """correct 接口处理 SQL 注入样式 → 不崩溃。"""
        svc = self._svc()
        for payload in ["' OR 1=1 --", "'; DROP TABLE--"]:
            corrected, degraded, _ = svc.correct(payload)
            assert degraded is False
            assert corrected is None or isinstance(corrected, str)

    def test_correct_emoji_no_crash(self):
        """correct 接口处理 emoji → 不崩溃。"""
        svc = self._svc()
        corrected, _, _ = svc.correct("🐱🐶")
        assert corrected is None or isinstance(corrected, str)

    # ---- 安全攻击 ----

    def test_path_traversal_no_crash(self):
        """q 含路径遍历字符 → 不崩溃，不泄露文件路径。"""
        svc = self._svc()
        for payload in ["../../etc/passwd", "..\\..\\windows\\system32", "/etc/shadow"]:
            suggestions, _, _ = svc.suggest(payload, 10)
            assert isinstance(suggestions, list)
            # 建议中不应包含文件路径
            for s in suggestions:
                assert ".." not in s
                assert "/" not in s
                assert "\\" not in s

    def test_correct_path_traversal_no_crash(self):
        """correct 接口处理路径遍历 → 不崩溃。"""
        svc = self._svc()
        corrected, _, _ = svc.correct("../../etc/passwd")
        assert corrected is None or isinstance(corrected, str)

    def test_xss_payload_no_crash(self):
        """q 含 XSS payload → 不崩溃。"""
        svc = self._svc()
        for payload in ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>"]:
            suggestions, _, _ = svc.suggest(payload, 10)
            assert isinstance(suggestions, list)

    # ---- 极端边界 ----

    def test_very_long_q_within_limit_no_crash(self):
        """q 500 字符（恰好上限）→ 不崩溃。"""
        svc = self._svc()
        suggestions, _, _ = svc.suggest("x" * 500, 10)
        assert isinstance(suggestions, list)

    def test_correct_exact_match_no_correction(self):
        """correct 完全匹配 → 返回 None（不需要纠正）。"""
        svc = self._svc()
        corrected, _, _ = svc.correct("nature")
        assert corrected is None

    def test_record_empty_query_ignored(self):
        """record_query 空字符串 → 被忽略。"""
        svc = self._svc()
        svc.record_query("")
        assert svc.top_n(10) == []

    def test_record_disabled_ignored(self):
        """SUGGEST_ENABLED=false 时 record_query 被忽略。"""
        svc = self._svc(suggest_enabled=False)
        svc.record_query("海滩")
        assert svc.top_n(10) == []


# ============================================================================
# 防御性测试报告
# ============================================================================

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """在 pytest 输出末尾生成防御性测试报告。"""
    stats = terminalreporter.stats

    passed = len(stats.get("passed", []))
    failed = len(stats.get("failed", []))
    errors = len(stats.get("error", []))
    skipped = len(stats.get("skipped", []))
    total = passed + failed + errors + skipped

    terminalreporter.write_line("")
    terminalreporter.write_line("=" * 72, yellow=True)
    terminalreporter.write_line("  防御性测试报告 — 混沌工程与安全渗透测试", yellow=True)
    terminalreporter.write_line("=" * 72, yellow=True)
    terminalreporter.write_line(f"  测试场景总数:  {total}")
    terminalreporter.write_line(f"  通过数:        {passed}", green=True)
    terminalreporter.write_line(f"  失败数:        {failed}", red=bool(failed))
    terminalreporter.write_line(f"  错误数:        {errors}", red=bool(errors))
    terminalreporter.write_line(f"  跳过数:        {skipped}")
    terminalreporter.write_line("")

    if failed == 0 and errors == 0:
        terminalreporter.write_line("  结论: 未发现系统漏洞（所有攻击场景均被正确防御）。", green=True)
    else:
        terminalreporter.write_line("  发现的系统漏洞清单:", red=True)
        terminalreporter.write_line("")

        severity_map = {
            "ssrf": "P0-严重",
            "localhost": "P0-严重",
            "leak": "P2-中危",
            "nan": "P1-高危",
            "crash": "P1-高危",
            "deadlock": "P1-高危",
            "maxsize": "P2-中危",
        }

        vuln_id = 0
        for failed_report in stats.get("failed", []):
            vuln_id += 1
            test_name = failed_report.nodeid
            failure_msg = str(failed_report.longrepr) if failed_report.longrepr else "未知"

            severity = "P3-低危"
            lower_msg = failure_msg.lower()
            for keyword, level in severity_map.items():
                if keyword in lower_msg or keyword in test_name.lower():
                    severity = level
                    break

            terminalreporter.write_line(f"  [{severity}] 漏洞 #{vuln_id}", red=True)
            terminalreporter.write_line(f"    测试: {test_name}")
            short_msg = failure_msg.split("\n")[0][:120] if failure_msg else "未知"
            terminalreporter.write_line(f"    描述: {short_msg}")
            terminalreporter.write_line(f"    复现: python -m pytest {test_name} -v")
            terminalreporter.write_line("")

        for error_report in stats.get("error", []):
            vuln_id += 1
            test_name = error_report.nodeid
            terminalreporter.write_line(f"  [P1-高危] 漏洞 #{vuln_id} (ERROR)", red=True)
            terminalreporter.write_line(f"    测试: {test_name}")
            terminalreporter.write_line(f"    复现: python -m pytest {test_name} -v")
            terminalreporter.write_line("")

    terminalreporter.write_line("=" * 72, yellow=True)