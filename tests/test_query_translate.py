"""中文查询 → 英文 CLIP 检索词翻译测试。"""
from __future__ import annotations

from app.scene.dictionary import get_scene_dict
from app.scene.query_translate import translate_query_for_clip


class TestTranslateQueryForClip:
    def test_mountain_query(self):
        translated, ok = translate_query_for_clip("巍峨群山", get_scene_dict())
        assert ok is True
        assert "a photo of mountains" in translated

    def test_multi_keyword_query(self):
        translated, ok = translate_query_for_clip("海边的猫咪", get_scene_dict())
        assert ok is True
        assert "cat" in translated
        assert "beach" in translated

    def test_sub_scene_before_scene(self):
        """子场景提示词应排在大场景前面（更具体优先）。"""
        translated, ok = translate_query_for_clip("海边度假", get_scene_dict())
        assert ok is True
        cat_idx = translated.find("beach")
        nature_idx = translated.find("natural scenery")
        assert 0 <= cat_idx < nature_idx

    def test_english_passthrough(self):
        assert translate_query_for_clip("mountain landscape") == ("mountain landscape", False)

    def test_unmatchable_chinese_passthrough(self):
        assert translate_query_for_clip("张三李四") == ("张三李四", False)

    def test_empty_query(self):
        assert translate_query_for_clip("") == ("", False)
        assert translate_query_for_clip("   ") == ("   ", False)

    def test_dictionary_label_match(self):
        """提示词表未命中时走词典中文 label 兜底。"""
        translated, ok = translate_query_for_clip("自然风光大全", get_scene_dict())
        assert ok is True
        assert "natural scenery" in translated

    def test_prompt_limit(self):
        """命中的提示词超过上限时应截断（防稀释 embedding）。"""
        # "海边的猫咪晚上在城市街道看烟花" 命中多个场景
        translated, ok = translate_query_for_clip(
            "海边的猫咪晚上在城市街道看烟花", get_scene_dict()
        )
        assert ok is True
        assert len(translated.split(", ")) <= 5

    def test_mixed_cn_en_translates(self):
        """中英混合查询仍翻译中文部分。"""
        translated, ok = translate_query_for_clip("好看的猫咪", get_scene_dict())
        assert ok is True
        assert "cat" in translated
