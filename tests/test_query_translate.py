"""中文查询 → 英文 CLIP 检索短语翻译测试（T4：词表组合，单一短语）。"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.admin import router as admin_router
from app.scene import query_translate as qt
from app.scene.query_translate import (
    query_has_cjk,
    translate_query_detailed,
    translate_query_for_clip,
    translate_query_for_search,
)


class TestUserReportedQueries:
    """线上实测 translated=False 的查询必须全部命中词表。"""

    def test_girl_smiling(self):
        english, ok = translate_query_for_clip("女孩微笑")
        assert ok is True
        assert english == "a photo of a smiling girl"

    def test_hot_pot(self):
        english, ok = translate_query_for_clip("火锅")
        assert ok is True
        assert "hot pot" in english

    def test_forbidden_city(self):
        english, ok = translate_query_for_clip("故宫")
        assert ok is True
        assert "forbidden city" in english.lower()

    def test_girlfriend(self):
        english, ok = translate_query_for_clip("女朋友")
        assert ok is True
        assert "girlfriend" in english

    def test_blue_car(self):
        english, ok = translate_query_for_clip("蓝色的车")
        assert ok is True
        assert english == "a photo of a blue car"

    def test_books(self):
        english, ok = translate_query_for_clip("书本")
        assert ok is True
        assert "book" in english

    def test_computer_desk(self):
        english, ok = translate_query_for_clip("电脑桌")
        assert ok is True
        assert "computer desk" in english

    def test_beach_sunset(self):
        """海边日落：日落语义必须保留（旧实现被泛化 prompt 稀释丢失）。"""
        english, ok = translate_query_for_clip("海边日落")
        assert ok is True
        assert "sunset" in english
        assert "beach" in english


class TestT9DictionaryGaps:
    """T9 验收发现的词库缺口：夜景城市/美食/雪景必须命中。"""

    def test_city_night(self):
        """夜景城市：城市为主体，夜景作时间方位附加。"""
        english, ok = translate_query_for_clip("夜景城市")
        assert ok is True
        assert english == "a photo of a city at night"

    def test_night_view_standalone(self):
        english, ok = translate_query_for_clip("夜景")
        assert ok is True
        assert "night view" in english

    def test_delicious_food(self):
        english, ok = translate_query_for_clip("美食")
        assert ok is True
        assert english == "a photo of delicious food"

    def test_snowy_landscape(self):
        english, ok = translate_query_for_clip("雪景")
        assert ok is True
        assert english == "a photo of a snowy landscape"

    def test_city_street_not_split_by_city(self):
        """回归守卫：新增短词「城市」不得抢占「城市街道」最长匹配。"""
        english, ok = translate_query_for_clip("城市街道")
        assert ok is True
        assert english == "a photo of a city street"


class TestSinglePhraseComposition:
    """输出必须是单条完整英文短语，禁止逗号拼接泛化 prompt。"""

    def test_no_comma_joined_prompts(self):
        english, ok = translate_query_for_clip("海边的猫咪晚上在城市街道看烟花")
        assert ok is True
        assert "," not in english
        assert english.count("a photo") == 1

    def test_head_final_primary_noun(self):
        """汉语中心语后置：最后匹配的主体作英文主体。"""
        detail = translate_query_detailed("海边日落")
        assert detail["nouns"][-1]["key"] == "日落"
        assert detail["english"] == "a photo of the sunset at the beach"

    def test_locative_attachment(self):
        english, ok = translate_query_for_clip("海边的猫咪")
        assert ok is True
        assert english == "a photo of a cat at the beach"

    def test_adjective_after_article(self):
        english, ok = translate_query_for_clip("微笑的女孩")
        assert ok is True
        assert english == "a photo of a smiling girl"

    def test_activity_as_locative(self):
        english, ok = translate_query_for_clip("游泳的女孩")
        assert ok is True
        assert english == "a photo of a girl swimming"

    def test_multiple_nouns_composition(self):
        english, ok = translate_query_for_clip("猫和狗")
        assert ok is True
        assert english == "a photo of a dog with a cat"

    def test_shot_prefix(self):
        english, ok = translate_query_for_clip("航拍的海边")
        assert ok is True
        assert english == "an aerial photo of the beach"

    def test_temporal_noun_standalone(self):
        english, ok = translate_query_for_clip("日落")
        assert ok is True
        assert "sunset" in english

    def test_mixed_location_query(self):
        english, ok = translate_query_for_clip("晚上的城市街道")
        assert ok is True
        assert english == "a photo of a city street at night"


class TestLongestMatch:
    def test_panda_overlaps_cat(self):
        """大熊猫（3 字）应整体命中，内部的猫不得重复参与组合。"""
        english, ok = translate_query_for_clip("大熊猫")
        assert ok is True
        assert "panda" in english
        assert "cat" not in english

    def test_single_char_noun_still_matches(self):
        english, ok = translate_query_for_clip("山")
        assert ok is True
        assert "mountain" in english


class TestPassthrough:
    def test_english_passthrough(self):
        assert translate_query_for_clip("mountain landscape") == (
            "mountain landscape",
            False,
        )

    def test_unmatchable_chinese_passthrough(self):
        assert translate_query_for_clip("张三李四") == ("张三李四", False)

    def test_adjective_only_not_translated(self):
        """仅修饰词无主体时不翻译（交给上层降级告警）。"""
        assert translate_query_for_clip("蓝色的") == ("蓝色的", False)

    def test_empty_query(self):
        assert translate_query_for_clip("") == ("", False)
        assert translate_query_for_clip("   ") == ("   ", False)

    def test_has_cjk(self):
        assert query_has_cjk("海边") is True
        assert query_has_cjk("beach") is False
        assert query_has_cjk("海边beach") is True


class TestDictionaryScale:
    """词表规模下限：防止悄悄退化回覆盖不足状态。"""

    def test_subjects_at_least_400(self):
        assert len(qt._NOUNS) >= 400

    def test_modifiers_at_least_200(self):
        assert len(qt._ADJ) >= 200

    def test_no_key_overlap_across_tables(self):
        assert not (set(qt._NOUNS) & set(qt._ADJ))
        assert not (set(qt._NOUNS) & set(qt._SHOT))
        assert not (set(qt._ADJ) & set(qt._SHOT))


class TestTranslateForSearch:
    async def test_dict_provider_default(self):
        settings = SimpleNamespace(translate_provider="dict")
        english, ok, provider = await translate_query_for_search(
            "海边日落", settings
        )
        assert ok is True
        assert provider == "dict"
        assert "sunset" in english

    async def test_english_skips_translation(self):
        settings = SimpleNamespace(translate_provider="dict")
        english, ok, provider = await translate_query_for_search(
            "beach sunset", settings
        )
        assert ok is False
        assert provider == "dict"
        assert english == "beach sunset"

    async def test_huawei_success(self, monkeypatch):
        async def fake_translate(text, settings):
            return "the beach at sunset"

        monkeypatch.setattr(qt, "_huawei_translate", fake_translate)
        settings = SimpleNamespace(translate_provider="huawei")
        english, ok, provider = await translate_query_for_search(
            "海边日落", settings
        )
        assert ok is True
        assert provider == "huawei"
        assert english == "the beach at sunset"

    async def test_huawei_failure_falls_back_to_dict(self, monkeypatch):
        async def broken_translate(text, settings):
            raise RuntimeError("NLP 不可用")

        monkeypatch.setattr(qt, "_huawei_translate", broken_translate)
        settings = SimpleNamespace(translate_provider="huawei")
        english, ok, provider = await translate_query_for_search(
            "海边日落", settings
        )
        assert ok is True
        assert provider == "dict"
        assert "sunset" in english


class TestTranslateEndpoint:
    def test_endpoint_returns_detail(self):
        app = FastAPI()
        app.include_router(admin_router)
        client = TestClient(app)
        resp = client.get("/admin/api/search/translate", params={"q": "海边日落"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["translated"] is True
        assert data["has_cjk"] is True
        assert "sunset" in data["english"]
        assert any(n["key"] == "海边" for n in data["nouns"])
        assert any(n["key"] == "日落" for n in data["nouns"])

    def test_endpoint_untranslated_query(self):
        app = FastAPI()
        app.include_router(admin_router)
        client = TestClient(app)
        resp = client.get("/admin/api/search/translate", params={"q": "张三李四"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["translated"] is False
        assert data["english"] is None
        assert data["has_cjk"] is True

    def test_endpoint_english_query(self):
        app = FastAPI()
        app.include_router(admin_router)
        client = TestClient(app)
        resp = client.get("/admin/api/search/translate", params={"q": "beach"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["translated"] is False
        assert data["has_cjk"] is False
