"""WatermarkEngine 单元测试：颜色解析、合成、平铺、格式兼容、文件处理。"""
from __future__ import annotations

import os

import pytest
from PIL import Image

from app.models.watermark import (
    ImageWatermarkStyle,
    TextWatermarkStyle,
    TileOptions,
    WatermarkPosition,
)
from app.services.watermark_engine import (
    WatermarkEngine,
    _clamp,
    _derive_output_path,
    _parse_color,
    apply_to_file,
)


# ---- _parse_color ----


def test_parse_color_rgb():
    """#RRGGBB 结合 opacity 转换为 RGBA。"""
    assert _parse_color("#FFFFFF", 1.0) == (255, 255, 255, 255)
    assert _parse_color("#FFFFFF", 0.5) == (255, 255, 255, 127)


def test_parse_color_rgba():
    """#RRGGBBAA 保留颜色自带 alpha，并与 opacity 相乘。"""
    assert _parse_color("#FF000080", 1.0) == (255, 0, 0, 128)
    assert _parse_color("#FF000080", 0.5) == (255, 0, 0, 64)


def test_parse_color_opacity_zero():
    """opacity=0 时 alpha 归零。"""
    assert _parse_color("#123456", 0.0) == (0x12, 0x34, 0x56, 0)


# ---- _clamp ----


def test_clamp_bounds():
    """值被钳制在 [low, high] 区间。"""
    assert _clamp(5) == 5
    assert _clamp(-1) == 0
    assert _clamp(300) == 255


# ---- _derive_output_path ----


def test_derive_output_path():
    """输出路径与源文件同目录，文件名追加 _watermarked 后缀。"""
    assert _derive_output_path("/a/b/photo.jpg") == os.path.join(
        "/a/b", "photo_watermarked.jpg"
    )


# ---- WatermarkEngine.render ----


def test_render_text_watermark_outputs_rgba():
    """RGB 输入经文字水印合成后输出 RGBA。"""
    source = Image.new("RGB", (200, 120), (30, 60, 90))
    style = TextWatermarkStyle(text="TEST", font_size=24)
    result = WatermarkEngine().render(source, style, None, None)
    assert result.mode == "RGBA"
    assert result.size == (200, 120)


def test_render_text_preserves_transparent_region():
    """合成后原透明区域保持透明（文字不污染无文字区域）。"""
    source = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    style = TextWatermarkStyle(
        text="WM", position=WatermarkPosition.BOTTOM_RIGHT, font_size=20
    )
    result = WatermarkEngine().render(source, style, None, None)
    assert result.getpixel((0, 0))[3] == 0


def test_render_text_draws_some_pixel():
    """文字水印确实绘制了非完全透明的像素。"""
    source = Image.new("RGBA", (200, 120), (255, 255, 255, 255))
    style = TextWatermarkStyle(
        text="HELLO", position=WatermarkPosition.CENTER, font_size=40,
        color="#000000", opacity=1.0,
    )
    result = WatermarkEngine().render(source, style, None, None)
    has_text = any(result.getpixel((x, y))[3] > 0 for x in range(200) for y in (50, 60, 70))
    assert has_text


def test_render_tile_watermark():
    """平铺水印基于百分比间距生成多个副本（非空白）。"""
    source = Image.new("RGBA", (300, 300), (255, 255, 255, 255))
    style = TextWatermarkStyle(
        text="T", position=WatermarkPosition.TILE, font_size=20,
        color="#000000", opacity=1.0, rotation=0,
    )
    tile = TileOptions(spacing_x_percent=40.0, spacing_y_percent=40.0)
    result = WatermarkEngine().render(source, style, None, tile)
    drawn = sum(1 for y in range(0, 300, 15) for x in range(0, 300, 15)
                if result.getpixel((x, y))[3] > 0)
    assert drawn > 0


def test_render_image_watermark(tmp_path):
    """图片水印正确缩放并合成到目标图片。"""
    wm_path = tmp_path / "logo.png"
    Image.new("RGBA", (50, 25), (255, 0, 0, 200)).save(wm_path, "PNG")

    source = Image.new("RGBA", (200, 200), (255, 255, 255, 255))
    style = ImageWatermarkStyle(
        image_path=str(wm_path), scale=0.25, opacity=1.0,
        position=WatermarkPosition.BOTTOM_RIGHT,
    )
    result = WatermarkEngine().render(source, None, style, None)
    # 右下角应出现带红色通道的像素。
    pixel = result.getpixel((180, 180))
    assert pixel[0] > 100


def test_render_image_watermark_missing_file():
    """水印图片不存在时抛出异常（由 apply_to_file 捕获）。"""
    source = Image.new("RGBA", (100, 100), (0, 0, 0, 255))
    style = ImageWatermarkStyle(image_path="/nonexistent/logo.png")
    with pytest.raises(FileNotFoundError):
        WatermarkEngine().render(source, None, style, None)


# ---- _to_rgba ----


@pytest.mark.parametrize("mode", ["L", "P", "RGB", "RGBA", "LA"])
def test_to_rgba(mode):
    """常见模式均转换为 RGBA。"""
    if mode == "P":
        img = Image.new("P", (10, 10))
    elif mode == "LA":
        img = Image.new("LA", (10, 10), (128, 128))
    else:
        img = Image.new(mode, (10, 10))
    assert WatermarkEngine._to_rgba(img).mode == "RGBA"


# ---- _compute_anchor ----


def test_compute_anchor_positions():
    """各锚点位置计算正确。"""
    size = (100, 60)
    bw, bh = 20, 10
    margin = 5
    pos = WatermarkEngine._compute_anchor
    assert pos(size, bw, bh, WatermarkPosition.TOP_LEFT, margin) == (5, 5)
    assert pos(size, bw, bh, WatermarkPosition.TOP_RIGHT, margin) == (75, 5)
    assert pos(size, bw, bh, WatermarkPosition.BOTTOM_LEFT, margin) == (5, 45)
    assert pos(size, bw, bh, WatermarkPosition.BOTTOM_RIGHT, margin) == (75, 45)
    assert pos(size, bw, bh, WatermarkPosition.CENTER, margin) == (40, 25)


# ---- apply_to_file ----


def test_apply_to_file_jpeg(tmp_path):
    """JPEG 端到端处理：生成同目录 _watermarked 输出。"""
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(src, "JPEG")
    result = apply_to_file(
        str(src), {"text": "wm", "font_size": 20}, None, None, None, False
    )
    assert result["success"] is True
    assert os.path.exists(result["output_path"])


def test_apply_to_file_no_overwrite(tmp_path):
    """输出已存在且未开启覆盖时返回失败。"""
    src = tmp_path / "photo.png"
    Image.new("RGBA", (50, 50), (0, 0, 0, 255)).save(src, "PNG")
    apply_to_file(str(src), {"text": "wm"}, None, None, None, False)
    result = apply_to_file(str(src), {"text": "wm"}, None, None, None, False)
    assert result["success"] is False
    assert "已存在" in result["error"]


def test_apply_to_file_missing_source(tmp_path):
    """源文件不存在返回明确错误。"""
    result = apply_to_file(
        str(tmp_path / "missing.jpg"), {"text": "wm"}, None, None, None, True
    )
    assert result["success"] is False
    assert result["error"] == "源文件不存在"


def test_apply_to_file_unidentified(tmp_path):
    """无法识别的文件返回明确错误。"""
    bad = tmp_path / "note.txt"
    bad.write_text("not an image")
    result = apply_to_file(str(bad), {"text": "wm"}, None, None, None, True)
    assert result["success"] is False
    assert "无法识别" in result["error"]


def test_apply_to_file_unsupported_format(tmp_path):
    """Pillow 能打开但白名单不支持的格式返回错误。"""
    src = tmp_path / "image.ppm"
    Image.new("RGB", (20, 20), (1, 2, 3)).save(src, "PPM")
    result = apply_to_file(str(src), {"text": "wm"}, None, None, None, True)
    assert result["success"] is False
    assert "不支持的图片格式" in result["error"]


def test_apply_to_file_no_style(tmp_path):
    """未提供任何样式返回错误。"""
    src = tmp_path / "photo.png"
    Image.new("RGBA", (20, 20), (0, 0, 0, 255)).save(src, "PNG")
    result = apply_to_file(str(src), None, None, None, None, True)
    assert result["success"] is False
    assert result["error"] == "未提供任何水印样式"