"""预置水印模板注册表与解析函数。

提供一组开箱即用的版权水印模板，以及按名称解析、按请求参数合并覆盖的
``resolve`` / ``merged_template`` 函数。解析均返回深拷贝，避免调用方意外
污染全局注册表。
"""
from __future__ import annotations

from ..models.watermark import (
    ImageWatermarkStyle,
    LicenseType,
    TextWatermarkStyle,
    TileOptions,
    WatermarkPosition,
    WatermarkTemplate,
)

# 授权类型 -> 版权水印默认文案。
LICENSE_TEXT: dict[LicenseType, str] = {
    LicenseType.NONE: "",
    LicenseType.COPYRIGHT: "© All rights reserved",
    LicenseType.CREATIVE_COMMONS: "CC BY-NC-ND 4.0",
    LicenseType.ALL_RIGHTS_RESERVED: "All Rights Reserved",
    LicenseType.COMMERCIAL: "Licensed for commercial use",
}

# 预置模板注册表：模板名 -> 模板定义。仅通过 resolve 访问，请勿直接改。
PRESET_TEMPLATES: dict[str, WatermarkTemplate] = {
    "copyright": WatermarkTemplate(
        name="copyright",
        license_type=LicenseType.COPYRIGHT,
        text=TextWatermarkStyle(
            text=LICENSE_TEXT[LicenseType.COPYRIGHT],
            position=WatermarkPosition.BOTTOM_RIGHT,
            font_size=32,
            color="#FFFFFF",
            opacity=0.75,
            rotation=0,
            margin_percent=4.0,
        ),
    ),
    "copyright-tile": WatermarkTemplate(
        name="copyright-tile",
        license_type=LicenseType.COPYRIGHT,
        text=TextWatermarkStyle(
            text=LICENSE_TEXT[LicenseType.COPYRIGHT],
            position=WatermarkPosition.TILE,
            font_size=28,
            color="#FFFFFF",
            opacity=0.35,
            rotation=-30,
        ),
        tile=TileOptions(spacing_x_percent=35.0, spacing_y_percent=35.0),
    ),
    "copyright-center": WatermarkTemplate(
        name="copyright-center",
        license_type=LicenseType.ALL_RIGHTS_RESERVED,
        text=TextWatermarkStyle(
            text=LICENSE_TEXT[LicenseType.ALL_RIGHTS_RESERVED],
            position=WatermarkPosition.CENTER,
            font_size=48,
            color="#FFFFFF",
            opacity=0.4,
            rotation=-15,
        ),
    ),
    "creative-commons": WatermarkTemplate(
        name="creative-commons",
        license_type=LicenseType.CREATIVE_COMMONS,
        text=TextWatermarkStyle(
            text=LICENSE_TEXT[LicenseType.CREATIVE_COMMONS],
            position=WatermarkPosition.BOTTOM_LEFT,
            font_size=30,
            color="#EEEEEE",
            opacity=0.8,
            rotation=0,
            margin_percent=4.0,
        ),
    ),
    "commercial": WatermarkTemplate(
        name="commercial",
        license_type=LicenseType.COMMERCIAL,
        text=TextWatermarkStyle(
            text=LICENSE_TEXT[LicenseType.COMMERCIAL],
            position=WatermarkPosition.TOP_RIGHT,
            font_size=30,
            color="#FFD700",
            opacity=0.8,
            rotation=0,
            margin_percent=4.0,
        ),
    ),
}


def available_templates() -> list[str]:
    """返回所有预置模板名称（按定义顺序）。"""
    return list(PRESET_TEMPLATES.keys())


def license_text(license_type: LicenseType) -> str:
    """返回指定授权类型对应的版权文案。"""
    return LICENSE_TEXT[license_type]


def resolve(template_name: str) -> WatermarkTemplate:
    """按名称解析预置模板并返回深拷贝。

    参数:
        template_name: 预置模板名称。

    返回:
        WatermarkTemplate 深拷贝实例。

    异常:
        ValueError: 模板名称不存在。
    """
    template = PRESET_TEMPLATES.get(template_name)
    if template is None:
        raise ValueError(f"未知水印模板: {template_name}")
    return template.model_copy(deep=True)


def merged_template(
    template_name: str,
    license_type: LicenseType | None = None,
    text: TextWatermarkStyle | None = None,
    image: ImageWatermarkStyle | None = None,
    tile: TileOptions | None = None,
) -> WatermarkTemplate:
    """解析模板并应用请求级覆盖，返回合并后的模板。

    合并规则：
        - ``text`` / ``image`` / ``tile`` 非空时整体覆盖模板对应样式；
        - ``license_type`` 非空时覆盖模板授权类型；若模板仍使用文字水印且未
          显式覆盖文字，则同步更新文字为对应授权类型文案（``NONE`` 时清空）。

    参数:
        template_name: 预置模板名称。
        license_type: 覆盖的授权类型（可选）。
        text: 覆盖的文字水印样式（可选）。
        image: 覆盖的图片水印样式（可选）。
        tile: 覆盖的平铺间距配置（可选）。

    返回:
        合并后的 WatermarkTemplate（深拷贝）。
    """
    merged = resolve(template_name)

    if license_type is not None:
        merged.license_type = license_type
        if text is None and merged.text is not None:
            if license_type == LicenseType.NONE:
                merged.text = None
            else:
                merged.text = merged.text.model_copy(
                    update={"text": LICENSE_TEXT[license_type]}
                )
    if text is not None:
        merged.text = text
    if image is not None:
        merged.image = image
    if tile is not None:
        merged.tile = tile

    if merged.text is None and merged.image is None:
        raise ValueError("合并后的模板不包含任何可见水印样式")
    return merged
