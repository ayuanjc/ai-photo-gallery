"""否定语义搜索单元测试。

覆盖：
    - parse_negations 各种否定表达解析
    - filter_by_negations 场景标签过滤
    - SearchService 端到端：否定解析 → 过滤 → applied_negations 回显
    - NEGATION_ENABLED=false 时行为与改动前一致
    - 0 个否定词时行为与改动前一致
"""
from __future__ import annotations

from app.core.errors import VisionError
from app.models.request import SearchRequest
from app.scene.models import SceneTags
from app.services.negation_parser import filter_by_negations, parse_negations
from app.services.search_service import SearchService
from app.services.vision.base import VisionProvider, VisionResult


class FakeImmich:
    def __init__(self, assets, raise_on_search=None):
        self.assets = assets
        self.raise_on_search = raise_on_search
        self.queries: list[tuple[str, int]] = []

    async def smart_search(self, query: str, k: int = 50, asset_ids=None):
        self.queries.append((query, k))
        if self.raise_on_search:
            raise self.raise_on_search
        return self.assets

    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"http://immich.test/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"http://immich.test/api/assets/{asset_id}/original"


class FakeVision(VisionProvider):
    name = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    async def analyze(self, image_url: str) -> VisionResult:
        self.calls.append(image_url)
        if self.fail:
            raise VisionError("vision boom")
        return VisionResult(labels=["海边", "日落"])


def _asset(aid: str, score: float, scene_tags: SceneTags | None = None) -> dict:
    asset = {
        "id": aid,
        "score": score,
        "exifInfo": {"dateTimeOriginal": "2023-06-15T10:00:00Z"},
    }
    if scene_tags is not None:
        asset["scene_tags"] = scene_tags
        asset["primary_scene"] = scene_tags.primary_scene
    return asset


def _request(**overrides) -> SearchRequest:
    payload = {"query": "海滩", "asset_ids": ["asset-001"]}
    payload.update(overrides)
    return SearchRequest(**payload)


# ========== parse_negations 测试 ==========


class TestParseNegations:
    def test_no_negation(self):
        q, neg = parse_negations("海边")
        assert q == "海边"
        assert neg == []

    def test_prefix_dont(self):
        q, neg = parse_negations("海边 不要人")
        assert q == "海边"
        assert neg == ["人"]

    def test_prefix_dont_no_space(self):
        q, neg = parse_negations("海边不要人")
        assert q == "海边"
        assert neg == ["人"]

    def test_prefix_dont_space_after(self):
        q, neg = parse_negations("海边 不要 人")
        assert q == "海边"
        assert neg == ["人"]

    def test_prefix_minus(self):
        q, neg = parse_negations("海边 -人")
        assert q == "海边"
        assert neg == ["人"]

    def test_prefix_meiyou(self):
        q, neg = parse_negations("海边 没有人")
        assert q == "海边"
        assert neg == ["人"]

    def test_prefix_buhan(self):
        q, neg = parse_negations("海边 不含人")
        assert q == "海边"
        assert neg == ["人"]

    def test_prefix_paichu(self):
        q, neg = parse_negations("海边 排除动物")
        assert q == "海边"
        assert neg == ["动物"]

    def test_multiple_negations(self):
        q, neg = parse_negations("海边 不要人 排除动物")
        assert q == "海边"
        assert neg == ["人", "动物"]

    def test_multiple_negations_no_space(self):
        q, neg = parse_negations("海边不要人排除动物")
        assert q == "海边"
        assert neg == ["人", "动物"]

    def test_empty_query(self):
        q, neg = parse_negations("")
        assert q == ""
        assert neg == []

    def test_only_prefix(self):
        q, neg = parse_negations("不要人")
        assert q == ""
        assert neg == ["人"]

    def test_prefix_at_end_no_token(self):
        q, neg = parse_negations("海边 不要")
        assert q == "海边"
        assert neg == []

    def test_custom_prefixes(self):
        q, neg = parse_negations("海边 NOT人", prefixes=["NOT"])
        assert q == "海边"
        assert neg == ["人"]

    def test_preserves_positive_after_negation(self):
        q, neg = parse_negations("不要人 海边")
        assert q == "海边"
        assert neg == ["人"]

    def test_whitespace_only(self):
        q, neg = parse_negations("   ")
        assert q == "   "
        assert neg == []


# ========== filter_by_negations 测试 ==========


class TestFilterByNegations:
    def test_empty_negations_returns_unchanged(self):
        assets = [_asset("a", 0.9), _asset("b", 0.8)]
        result = filter_by_negations(assets, [])
        assert result is assets

    def test_empty_assets(self):
        result = filter_by_negations([], ["人"])
        assert result == []

    def test_filter_by_scene_label_substring(self):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
        ]
        result = filter_by_negations(assets, ["人"])
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_filter_by_scene_key(self):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
        ]
        result = filter_by_negations(assets, ["people"])
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_filter_by_sub_scene(self):
        dog_tags = SceneTags(primary_scene="animal", scenes=["animal"], sub_scenes=["dog"])
        cat_tags = SceneTags(primary_scene="animal", scenes=["animal"], sub_scenes=["cat"])
        assets = [
            _asset("a", 0.9, cat_tags),
            _asset("b", 0.8, dog_tags),
        ]
        result = filter_by_negations(assets, ["dog"])
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_filter_multiple_negations(self):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        animal_tags = SceneTags(primary_scene="animal", scenes=["animal"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
            _asset("c", 0.7, animal_tags),
        ]
        result = filter_by_negations(assets, ["人", "动物"])
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_no_scene_tags_not_filtered(self):
        assets = [
            _asset("a", 0.9),
            _asset("b", 0.8),
        ]
        result = filter_by_negations(assets, ["人"])
        assert len(result) == 2

    def test_preserves_order(self):
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
            _asset("c", 0.7, nature_tags),
        ]
        result = filter_by_negations(assets, ["人"])
        assert [r["id"] for r in result] == ["a", "c"]


# ========== SearchService 端到端测试 ==========


class TestSearchServiceNegation:
    async def test_negation_filtering_in_search(self, settings):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边 不要人"))

        assert resp.total == 1
        assert resp.results[0].asset_id == "a"
        assert resp.applied_negations == ["人"]

    async def test_no_negation_returns_all(self, settings):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边"))

        assert resp.total == 2
        assert resp.applied_negations == []

    async def test_negation_disabled(self, settings):
        settings.negation_enabled = False
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边 不要人"))

        assert resp.total == 2
        assert resp.applied_negations == []

    async def test_explicit_negations_field(self, settings):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边", negations=["人"]))

        assert resp.total == 1
        assert resp.results[0].asset_id == "a"
        assert resp.applied_negations == ["人"]

    async def test_combined_parsed_and_explicit_negations(self, settings):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        animal_tags = SceneTags(primary_scene="animal", scenes=["animal"])
        nature_tags = SceneTags(primary_scene="nature", scenes=["nature"])
        assets = [
            _asset("a", 0.9, nature_tags),
            _asset("b", 0.8, people_tags),
            _asset("c", 0.7, animal_tags),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边 不要人", negations=["动物"]))

        assert resp.total == 1
        assert resp.results[0].asset_id == "a"
        assert "人" in resp.applied_negations
        assert "动物" in resp.applied_negations

    async def test_cleaned_query_used_for_search(self, settings):
        assets = [_asset("a", 0.9)]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        await svc.search(_request(query="海边 不要人"))

        assert len(immich.queries) == 1
        query_sent, _ = immich.queries[0]
        assert "不要" not in query_sent
        assert "人" not in query_sent or "海边" in query_sent

    async def test_negation_with_no_scene_tags(self, settings):
        assets = [
            _asset("a", 0.9),
            _asset("b", 0.8),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边 不要人"))

        assert resp.total == 2
        assert resp.applied_negations == ["人"]

    async def test_all_filtered_returns_empty(self, settings):
        people_tags = SceneTags(primary_scene="people", scenes=["people"])
        assets = [
            _asset("a", 0.9, people_tags),
            _asset("b", 0.8, people_tags),
        ]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海边 不要人"))

        assert resp.total == 0
        assert resp.applied_negations == ["人"]

    async def test_enriched_query_uses_cleaned_query(self, settings):
        assets = [_asset("a", 0.9)]
        immich = FakeImmich(assets)
        vision = FakeVision()
        svc = SearchService(settings, immich, vision)

        resp = await svc.search(_request(query="海滩 不要人", asset_ids=["asset-001"]))

        if resp.enriched_query:
            assert "不要" not in resp.enriched_query
