"""水印渲染引擎（核心合成逻辑）。

设计要点：
    - 文字水印始终先在独立的 RGBA 透明画布上绘制（支持旋转、透明度），
      再通过 ``Image.alpha_composite`` 合成到目标图片，绝不在 RGB 底图上
      直接绘制文字，从而保留目标图片的 alpha 通道并避免像素污染。
    - 平铺水印的行列间距基于目标图片尺寸的百分比动态计算，不依赖固定像素，
      以适配不同分辨率。
    - 模块级函数 ``apply_to_file`` 为进程池入口：在子进程中完成
      「打开 → 方向校正 → 转换 → 渲染 → 保存」，避免主进程被 Pillow 阻塞。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from ..models.watermark import (
    ImageWatermarkStyle,
    TextWatermarkStyle,
    TileOptions,
    WatermarkPosition,
)

logger = logging.getLogger(__name__)

# 支持的输入图片格式白名单（Pillow format 名称，大写）。
_SUPPORTED_INPUT_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"}

# 无 alpha 通道、保存为 JPEG 时需先转 RGB 的输出扩展名集合。
_JPEG_EXTENSIONS = {".jpg", ".jpeg"}


def _parse_color(color: str, opacity: float) -> tuple[int, int, int, int]:
    """将十六进制颜色字符串与不透明度因子合并为 RGBA 整数元组。

    参数:
        color: ``#RRGGBB`` 或 ``#RRGGBBAA`` 颜色字符串。
        opacity: 0~1 的不透明度因子，与颜色自带 alpha 相乘。

    返回:
        ``(r, g, b, a)`` 整数元组。
    """
    hex_part = color.lstrip("#")
    if len(hex_part) == 6:
        r, g, b = int(hex_part[0:2], 16), int(hex_part[2:4], 16), int(hex_part[4:6], 16)
        a = 255
    else:
        r, g, b = int(hex_part[0:2], 16), int(hex_part[2:4], 16), int(hex_part[4:6], 16)
        a = int(hex_part[6:8], 16)
    return (r, g, b, _clamp(int(a * opacity)))


def _clamp(value: int, low: int = 0, high: int = 255) -> int:
    """将整数限制在 ``[low, high]`` 区间内。"""
    return max(low, min(high, value))


def _load_font(font_path: str | None, font_size: int) -> ImageFont.ImageFont:
    """加载字体：优先自定义路径，否则回退到 Pillow 默认字体。

    参数:
        font_path: 自定义字体文件路径（可选）。
        font_size: 字体大小（像素）。

    返回:
        可绘制文字的 ``ImageFont`` 对象。

    异常:
        ValueError: 字体路径非空但无法加载。
    """
    if font_path:
        try:
            return ImageFont.truetype(font_path, font_size)
        except OSError as exc:
            raise ValueError(f"字体文件无法加载: {font_path}") from exc
    return ImageFont.load_default(size=font_size)


def _derive_output_path(source_path: str) -> str:
    """推导输出文件路径：在文件名后追加 ``_watermarked`` 后缀，同目录输出。"""
    directory = os.path.dirname(source_path) or "."
    base, ext = os.path.splitext(os.path.basename(source_path))
    return os.path.join(directory, f"{base}_watermarked{ext}")


def _save_image(image: Image.Image, output_path: str) -> None:
    """按扩展名保存图片，JPEG 自动做 RGBA→RGB（白底）降级。"""
    ext = os.path.splitext(output_path)[1].lower()
    if ext in _JPEG_EXTENSIONS:
        if image.mode != "RGB":
            background = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "RGBA":
                background.paste(image, mask=image.split()[3])
            else:
                background.paste(image)
            image = background
        image.save(output_path, "JPEG", quality=95)
    elif ext == ".png":
        image.save(output_path, "PNG")
    elif ext == ".webp":
        image.save(output_path, "WEBP")
    else:
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[3])
            image = background
        image.save(output_path)


class WatermarkEngine:
    """水印渲染引擎：在内存中完成文字/图片水印合成。"""

    def __init__(self, default_font_path: str | None = None) -> None:
        self._default_font_path = default_font_path

    def render(
        self,
        source: Image.Image,
        text: TextWatermarkStyle | None,
        image: ImageWatermarkStyle | None,
        tile: TileOptions | None,
    ) -> Image.Image:
        """在源图上依次合成文字与图片水印，返回新图片。

        参数:
            source: 源图片（任意 Pillow 支持的模式，内部统一转 RGBA）。
            text: 文字水印样式（可选）。
            image: 图片水印样式（可选）。
            tile: 平铺间距配置（样式位置为 TILE 时使用）。

        返回:
            合成水印后的 RGBA 图片。
        """
        base = self._to_rgba(source)

        if text is not None:
            block = self._render_text_block(text)
            layer = self._place_block(base.size, block, text.position, text.margin_percent, tile)
            base = Image.alpha_composite(base, layer)

        if image is not None:
            block = self._render_image_block(image, base.size)
            layer = self._place_block(base.size, block, image.position, image.margin_percent, tile)
            base = Image.alpha_composite(base, layer)

        return base

    @staticmethod
    def _to_rgba(image: Image.Image) -> Image.Image:
        """将任意模式图片转换为 RGBA（RGB/L/P/LA/CMYK 等均可处理）。"""
        if image.mode == "RGBA":
            return image
        return image.convert("RGBA")

    def _load_font(self, style: TextWatermarkStyle) -> ImageFont.ImageFont:
        """根据样式加载文字字体。"""
        return _load_font(style.font_path or self._default_font_path, style.font_size)

    def _render_text_block(self, style: TextWatermarkStyle) -> Image.Image:
        """在独立 RGBA 透明画布上渲染单个文字水印块（含旋转与透明度）。"""
        font = self._load_font(style)
        fill = _parse_color(style.color, style.opacity)

        # 测量文字实际包围盒。
        measuring = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        left, top, right, bottom = ImageDraw.Draw(measuring).textbbox(
            (0, 0), style.text, font=font
        )
        text_w = max(1, right - left)
        text_h = max(1, bottom - top)

        # 独立透明画布（含少量 padding 以容纳字形溢出）。
        pad = 4
        canvas = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.text((pad - left, pad - top), style.text, font=font, fill=fill)

        # 旋转（expand 使画布自适应扩大，避免文字被裁切）。
        if style.rotation:
            canvas = canvas.rotate(style.rotation, expand=True, resample=Image.Resampling.BICUBIC)
        return canvas

    def _render_image_block(self, style: ImageWatermarkStyle, target_size: tuple[int, int]) -> Image.Image:
        """加载并缩放图片水印块：缩放比例以目标图片短边为基准。"""
        with Image.open(style.image_path) as watermark:
            watermark = watermark.convert("RGBA")
            short_side = min(target_size)
            target_w = max(1, int(short_side * style.scale))
            ratio = target_w / max(1, watermark.width)
            target_h = max(1, int(watermark.height * ratio))
            watermark = watermark.resize((target_w, target_h), Image.Resampling.LANCZOS)
            if style.opacity < 1.0:
                alpha = watermark.split()[3].point(
                    lambda a: int(a * style.opacity)
                )
                watermark.putalpha(alpha)
            return watermark

    def _place_block(
        self,
        size: tuple[int, int],
        block: Image.Image,
        position: WatermarkPosition,
        margin_percent: float,
        tile: TileOptions | None,
    ) -> Image.Image:
        """将单个水印块按位置（单点或平铺）放置到整幅透明图层上。"""
        layer = Image.new("RGBA", size, (0, 0, 0, 0))

        if position == WatermarkPosition.TILE:
            # 平铺间距基于目标图片尺寸百分比动态计算，并保证不小于块尺寸以免重叠。
            sx_pct = tile.spacing_x_percent if tile is not None else 30.0
            sy_pct = tile.spacing_y_percent if tile is not None else 30.0
            step_x = max(block.width + 1, int(size[0] * sx_pct / 100))
            step_y = max(block.height + 1, int(size[1] * sy_pct / 100))
            y = 0
            while y < size[1]:
                x = 0
                while x < size[0]:
                    layer.paste(block, (x, y), block)
                    x += step_x
                y += step_y
        else:
            margin = int(min(size) * margin_percent / 100)
            x, y = self._compute_anchor(size, block.width, block.height, position, margin)
            layer.paste(block, (x, y), block)
        return layer

    @staticmethod
    def _compute_anchor(
        size: tuple[int, int],
        block_w: int,
        block_h: int,
        position: WatermarkPosition,
        margin: int,
    ) -> tuple[int, int]:
        """计算水印块的左上角坐标，保证不超出图片边界。"""
        width, height = size
        if position == WatermarkPosition.TOP_LEFT:
            anchor = (margin, margin)
        elif position == WatermarkPosition.TOP_RIGHT:
            anchor = (width - block_w - margin, margin)
        elif position == WatermarkPosition.BOTTOM_LEFT:
            anchor = (margin, height - block_h - margin)
        elif position == WatermarkPosition.BOTTOM_RIGHT:
            anchor = (width - block_w - margin, height - block_h - margin)
        elif position == WatermarkPosition.CENTER:
            anchor = ((width - block_w) // 2, (height - block_h) // 2)
        else:
            anchor = (margin, margin)
        return (_clamp(anchor[0], 0, max(0, width - block_w)),
                _clamp(anchor[1], 0, max(0, height - block_h)))


def apply_to_file(
    source_path: str,
    text_style_dict: dict[str, Any] | None,
    image_style_dict: dict[str, Any] | None,
    tile_dict: dict[str, Any] | None,
    output_path: str | None,
    overwrite: bool,
    default_font_path: str | None = None,
) -> dict[str, Any]:
    """子进程入口：打开源图、应用水印并保存，返回结构化结果。

    供 ``ProcessPoolExecutor`` 在子进程中调用；所有参数均为可 pickle 的
    简单类型（dict / str / bool）。

    参数:
        source_path: 源图片路径。
        text_style_dict: 文字水印样式字典（可选）。
        image_style_dict: 图片水印样式字典（可选）。
        tile_dict: 平铺间距字典（可选）。
        output_path: 输出路径（可选，缺省同目录追加后缀）。
        overwrite: 输出已存在时是否覆盖。
        default_font_path: 默认字体路径（可选）。

    返回:
        形如 ``{"source_path", "success", "output_path", "error"}`` 的字典。
    """
    try:
        text_style = TextWatermarkStyle(**text_style_dict) if text_style_dict else None
        image_style = ImageWatermarkStyle(**image_style_dict) if image_style_dict else None
        tile = TileOptions(**tile_dict) if tile_dict else None
    except Exception as exc:  # noqa: BLE001 - 样式校验失败转为逐项错误
        return {"source_path": source_path, "success": False, "output_path": None, "error": f"水印样式无效: {exc}"}

    if text_style is None and image_style is None:
        return {"source_path": source_path, "success": False, "output_path": None, "error": "未提供任何水印样式"}

    out_path = output_path or _derive_output_path(source_path)

    try:
        if not os.path.isfile(source_path):
            return {"source_path": source_path, "success": False, "output_path": None, "error": "源文件不存在"}
        if os.path.exists(out_path) and not overwrite:
            return {"source_path": source_path, "success": False, "output_path": None, "error": f"输出文件已存在: {out_path}"}
    except OSError as exc:
        return {"source_path": source_path, "success": False, "output_path": None, "error": f"路径检查失败: {exc}"}

    try:
        with Image.open(source_path) as source:
            if source.format not in _SUPPORTED_INPUT_FORMATS:
                return {
                    "source_path": source_path,
                    "success": False,
                    "output_path": None,
                    "error": f"不支持的图片格式: {source.format}",
                }
            source = ImageOps.exif_transpose(source)
            engine = WatermarkEngine(default_font_path)
            result = engine.render(source, text_style, image_style, tile)
            _save_image(result, out_path)
    except UnidentifiedImageError as exc:
        return {"source_path": source_path, "success": False, "output_path": None, "error": f"无法识别的图片文件: {exc}"}
    except Exception as exc:  # noqa: BLE001 - 单文件失败转为逐项错误
        logger.warning("水印处理失败 file=%s err=%s", source_path, exc)
        return {"source_path": source_path, "success": False, "output_path": None, "error": str(exc)}

    return {"source_path": source_path, "success": True, "output_path": out_path, "error": None}
