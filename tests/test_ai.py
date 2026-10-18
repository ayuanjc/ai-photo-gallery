"""AI 分析（同一个人 vs 相似图片）单元与集成测试。"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.ai.base import AnalysisResult, categorize
from app.ai.factory import build_ai_analyzer
from app.ai.huawei_analyzer import HuaweiFaceAnalyzer
from app.ai.immich_analyzer import ImmichAnalyzer
from app.api.deps import get_ai_analyzer, get_settings_dep
from app.core.config import Settings
from app.core.errors import (
    AIAnalyzerTimeoutError,
    AIAnalyzerUnavailableError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.main import app


def _url(asset_id: str) -> str:
    return f"http://immich.local/api/assets/{asset_id}/thumbnail"


REF = "aaaaaaaaaaaaaaaa"
CAND = "bbbbbbbbbbbbbbbb"


class FakeImmich:
    """模拟 Immich 资产 / 人物 / 相似度查询。"""

    def __init__(self) -> None:
        self.assets: dict[str, dict] = {}
        self.similar: dict[tuple[str, str], float] = {}
        self.people: list[dict] = []
        self.fail_get_asset: str | None = None  # unavailable | timeout | boom
        self.fail_similar: str | None = None

    def set_asset(self, asset_id: str, person_id: str | None) -> None:
        asset: dict = {"id": asset_id}
        if person_id:
            asset["people"] = [{"id": person_id}]
        self.assets[asset_id] = asset

    def set_similar(self, ref_id: str, cand_id: str, score: float) -> None:
        self.similar[(ref_id, cand_id)] = score

    async def get_asset(self, asset_id: str) -> dict:
        if self.fail_get_asset == "unavailable":
            raise UpstreamUnavailableError("immich down")
        if self.fail_get_asset == "timeout":
            raise UpstreamTimeoutError("immich timeout")
        if self.fail_get_asset == "boom":
            raise RuntimeError("boom")
        return self.assets.get(asset_id, {})

    async def similar_assets(self, asset_id: str, k: int = 50) -> list[dict]:
        if self.fail_similar == "unavailable":
            raise UpstreamUnavailableError("immich down")
        if self.fail_similar == "timeout":
            raise UpstreamTimeoutError("immich timeout")
        return [
            {"id": cid, "score": score}
            for (rid, cid), score in self.similar.items()
            if rid == asset_id
        ]

    async def list_people(self) -> list[dict]:
        return list(self.people)


def _analyzer(immich: FakeImmich, **settings_kw) -> ImmichAnalyzer:
    settings = Settings(**settings_kw)
    return ImmichAnalyzer(settings, immich)  # type: ignore[arg-type]


# ---- 分类纯函数（阈值边界）----

def test_categorize_same_person_at_threshold():
    category, _ = categorize(True, 0.6, 0.5, person_threshold=0.6, similarity_threshold=0.75)
    assert category == "same_person"


def test_categorize_conf_below_threshold_falls_to_similar():
    category, _ = categorize(True, 0.599, 0.8, person_threshold=0.6, similarity_threshold=0.75)
    assert category == "similar_image"


def test_categorize_similar_at_threshold():
    category, _ = categorize(False, 0.0, 0.75, person_threshold=0.6, similarity_threshold=0.75)
    assert category == "similar_image"


def test_categorize_unrelated():
    category, _ = categorize(False, 0.0, 0.749, person_threshold=0.6, similarity_threshold=0.75)
    assert category == "unrelated"


def test_categorize_conf_below_and_low_similar_is_unrelated():
    category, _ = categorize(True, 0.5, 0.4, person_threshold=0.6, similarity_threshold=0.75)
    assert category == "unrelated"


# ---- ImmichAnalyzer 单元 ----

async def test_same_person():
    immich = FakeImmich()
    immich.set_asset(REF, "person-1")
    immich.set_asset(CAND, "person-1")
    immich.set_similar(REF, CAND, 0.3)

    r = await _analyzer(immich).analyze(_url(REF), _url(CAND))

    assert r.same_person is True
    assert r.category == "same_person"


async def test_similar_image_no_face():
    immich = FakeImmich()
    immich.set_asset(REF, None)
    immich.set_asset(CAND, None)
    immich.set_similar(REF, CAND, 0.9)

    r = await _analyzer(immich).analyze(_url(REF), _url(CAND))

    assert r.same_person is False
    assert r.person_confidence == 0.0
    assert r.category == "similar_image"


async def test_unrelated():
    immich = FakeImmich()
    immich.set_asset(REF, None)
    immich.set_asset(CAND, None)
    immich.set_similar(REF, CAND, 0.3)

    r = await _analyzer(immich).analyze(_url(REF), _url(CAND))

    assert r.category == "unrelated"


async def test_no_face_not_misclassified_as_same_person():
    immich = FakeImmich()
    immich.set_asset(REF, None)
    immich.set_asset(CAND, None)
    immich.set_similar(REF, CAND, 1.0)  # 即便画面极相似

    r = await _analyzer(immich).analyze(_url(REF), _url(CAND))

    assert r.same_person is False
    assert r.person_confidence == 0.0


async def test_different_person_high_similarity_is_similar_image():
    immich = FakeImmich()
    immich.set_asset(REF, "person-1")
    immich.set_asset(CAND, "person-2")
    immich.set_similar(REF, CAND, 0.9)

    r = await _analyzer(immich).analyze(_url(REF), _url(CAND))

    assert r.same_person is False
    assert r.category == "similar_image"


async def test_provider_business_error_degrades():
    immich = FakeImmich()
    immich.set_asset(REF, "person-1")
    immich.set_asset(CAND, "person-1")
    immich.fail_get_asset = "boom"

    r = await _analyzer(immich, ai_analyzer_max_retries=0).analyze(_url(REF), _url(CAND))

    assert r.category == "unrelated"
    assert r.same_person is False
    assert "降级" in r.reason


async def test_provider_unavailable_raises_502():
    immich = FakeImmich()
    immich.fail_get_asset = "unavailable"

    with pytest.raises(AIAnalyzerUnavailableError):
        await _analyzer(immich, ai_analyzer_max_retries=0).analyze(_url(REF), _url(CAND))


async def test_provider_timeout_raises_504():
    immich = FakeImmich()
    immich.fail_get_asset = "timeout"

    with pytest.raises(AIAnalyzerTimeoutError):
        await _analyzer(immich, ai_analyzer_max_retries=0).analyze(_url(REF), _url(CAND))


async def test_unresolvable_url_degrades():
    immich = FakeImmich()
    r = await _analyzer(immich, ai_analyzer_max_retries=0).analyze(
        "https://example.com/plain.jpg", "https://example.com/other.jpg"
    )
    assert r.category == "unrelated"
    assert "无法从 URL 解析" in r.reason


async def test_health_check():
    immich = FakeImmich()
    assert await _analyzer(immich).health_check() is True


async def test_health_check_false_when_upstream_down():
    immich = FakeImmich()
    immich.fail_get_asset = "unavailable"
    # list_people 不在这支里，直接让 list_people 失败
    orig = immich.list_people

    async def _fail():
        raise UpstreamUnavailableError("down")

    immich.list_people = _fail  # type: ignore[method-assign]
    assert await _analyzer(immich).health_check() is False
    immich.list_people = orig  # type: ignore[method-assign]


# ---- 工厂 / 华为云占位 ----

def test_factory_returns_immich_by_default():
    a = build_ai_analyzer(Settings(), FakeImmich())  # type: ignore[arg-type]
    assert a.name == "immich"


def test_factory_returns_huawei():
    a = build_ai_analyzer(Settings(ai_analyzer_provider="huawei"), FakeImmich())  # type: ignore[arg-type]
    assert isinstance(a, HuaweiFaceAnalyzer)


async def test_huawei_not_implemented():
    a = HuaweiFaceAnalyzer(Settings())
    with pytest.raises(AIAnalyzerUnavailableError):
        await a.analyze("a", "b")


async def test_huawei_health_check_returns_false():
    a = HuaweiFaceAnalyzer(Settings())
    assert await a.health_check() is False


# ---- 集成测试 ----

class FakeAnalyzer:
    name = "fake"

    def __init__(self, results=None, exc: Exception | None = None) -> None:
        self.results = results or []
        self.exc = exc

    async def analyze(self, reference_url: str, candidate_url: str) -> AnalysisResult:
        if self.exc is not None:
            raise self.exc
        return AnalysisResult(
            same_person=False,
            person_confidence=0.0,
            visual_similarity=0.0,
            category="unrelated",
            reason="fake",
        )

    async def health_check(self) -> bool:
        return True


@contextmanager
def make_client(analyzer, search_api_key: str = ""):
    app.dependency_overrides[get_settings_dep] = lambda: Settings(search_api_key=search_api_key)
    app.dependency_overrides[get_ai_analyzer] = lambda: analyzer
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _body():
    return {
        "reference_url": "https://example.com/ref.jpg",
        "candidate_urls": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
    }


def test_analyze_normal_returns_list():
    with make_client(FakeAnalyzer()) as c:
        resp = c.post("/api/v1/ai/analyze", json=_body())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["category"] == "unrelated"


def test_analyze_requires_api_key():
    with make_client(FakeAnalyzer(), search_api_key="secret") as c:
        resp = c.post("/api/v1/ai/analyze", json=_body())
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_analyze_with_correct_api_key():
    with make_client(FakeAnalyzer(), search_api_key="secret") as c:
        resp = c.post(
            "/api/v1/ai/analyze", json=_body(), headers={"x-api-key": "secret"}
        )
    assert resp.status_code == 200


def test_analyze_upstream_timeout_returns_504():
    analyzer = FakeAnalyzer(exc=AIAnalyzerTimeoutError("timeout"))
    with make_client(analyzer) as c:
        resp = c.post("/api/v1/ai/analyze", json=_body())
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "AI_ANALYZER_TIMEOUT"


def test_analyze_upstream_unavailable_returns_502():
    analyzer = FakeAnalyzer(exc=AIAnalyzerUnavailableError("down"))
    with make_client(analyzer) as c:
        resp = c.post("/api/v1/ai/analyze", json=_body())
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "AI_ANALYZER_UNAVAILABLE"


def test_analyze_too_many_candidates_returns_400():
    with make_client(FakeAnalyzer()) as c:
        resp = c.post(
            "/api/v1/ai/analyze",
            json={
                "reference_url": "https://example.com/ref.jpg",
                "candidate_urls": [f"https://example.com/{i}.jpg" for i in range(21)],
            },
        )
    assert resp.status_code == 400
