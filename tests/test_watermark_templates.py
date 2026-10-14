"""预置水印模板注册表与解析函数单元测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.watermark import (
    ImageWatermarkStyle,
    LicenseType,
    TextWatermarkStyle,
    TileOptions,
    WatermarkPosition,
    WatermarkTemplate,
)
from app.services.watermark_templates import (
    LICENSE_TEXT,
    available_templates,
    license_text,
    merged_template,
    resolve,
)


def test_available_templates_non_empty():
    """存在预置模板。"""
    templates = available_templates()
    assert "copyright" in templates
    assert "copyright-tile" in templates


def test_license_text_coverage():
    """每个授权类型都有对应文案。"""
    for license_type in LicenseType:
        assert isinstance(license_text(license_type), str)


def test_resolve_known_template():
    """解析已知模板返回正确名称与授权类型。"""
    template = resolve("copyright")
    assert template.name == "copyright"
    assert template.license_type == LicenseType.COPYRIGHT
    assert template.text is not None


def test_resolve_unknown_template():
    """解析未知模板抛出 ValueError。"""
    with pytest.raises(ValueError, match="未知水印模板"):
        resolve("does-not-exist")


def test_resolve_returns_deep_copy():
    """解析结果为深拷贝，修改不影响注册表。"""
    template = resolve("copyright")
    template.text.text = "MUTATED"
    fresh = resolve("copyright")
    assert fresh.text.text != "MUTATED"


def test_merged_template_license_override():
    """license_type 覆盖时同步更新文字文案。"""
    merged = merged_template("copyright", license_type=LicenseType.COMMERCIAL)
    assert merged.license_type == LicenseType.COMMERCIAL
    assert merged.text.text == LICENSE_TEXT[LicenseType.COMMERCIAL]


def test_merged_template_text_override():
    """显式文字样式覆盖模板样式，且不受 license 覆盖影响。"""
    custom = TextWatermarkStyle(text="MY CUSTOM")
    merged = merged_template(
        "copyright", license_type=LicenseType.COMMERCIAL, text=custom
    )
    assert merged.text.text == "MY CUSTOM"


def test_merged_template_image_override():
    """图片水印样式覆盖模板样式。"""
    image = ImageWatermarkStyle(image_path="/tmp/logo.png")
    merged = merged_template("copyright", image=image)
    assert merged.image is not None
    assert merged.image.image_path == "/tmp/logo.png"


def test_merged_template_tile_override():
    """平铺间距配置覆盖模板配置。"""
    tile = TileOptions(spacing_x_percent=50.0, spacing_y_percent=50.0)
    merged = merged_template("copyright-tile", tile=tile)
    assert merged.tile.spacing_x_percent == 50.0


def test_merged_template_none_license_clears_text():
    """license_type=NONE 时清空版权文案。"""
    merged = merged_template("copyright", license_type=LicenseType.NONE)
    assert merged.text.text == ""


def test_template_requires_visible_style():
    """模板必须包含文字或图片样式至少其一。"""
    with pytest.raises(ValidationError):
        WatermarkTemplate(name="empty", license_type=LicenseType.COPYRIGHT)


def test_text_style_invalid_color():
    """非法颜色字符串被拒绝。"""
    with pytest.raises(ValidationError):
        TextWatermarkStyle(text="x", color="red")