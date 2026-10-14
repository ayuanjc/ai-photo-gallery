"""水印管理的请求/响应数据模型、模板样式模型与授权类型枚举。

本模块仅定义数据契约，不包含任何图像处理逻辑。所有样式字段均带类型与
范围约束，便于 API 层在进入服务层之前完成参数校验。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class LicenseType(str, Enum):
    """图片授权类型枚举，用于渲染对应的版权水印文案。"""

    NONE = "none"
    COPYRIGHT = "copyright"
    CREATIVE_COMMONS = "creative_commons"
    ALL_RIGHTS_RESERVED = "all_rights_reserved"
    COMMERCIAL = "commercial"


class WatermarkPosition(str, Enum):
    """水印在目标图片上的锚定位置。"""

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    CENTER = "center"
    TILE = "tile"


class TextWatermarkStyle(BaseModel):
    """文字水印样式。

    颜色采用十六进制字符串（``#RRGGBB`` 或 ``#RRGGBBAA``），实际 RGBA 转换
    在渲染引擎中完成；``opacity`` 为额外的不透明度因子（0~1），与颜色自带
    alpha 相乘得到最终透明度。
    """

    text: str = Field(..., min_length=1, max_length=500, description="水印文字内容")
    position: WatermarkPosition = Field(
        default=WatermarkPosition.BOTTOM_RIGHT, description="水印位置"
    )
    font_size: int = Field(default=36, ge=8, le=512, description="字体大小（像素）")
    color: str = Field(
        default="#FFFFFF", min_length=4, max_length=9, description="文字颜色（十六进制）"
    )
    opacity: float = Field(default=0.7, ge=0.0, le=1.0, description="文字不透明度（0~1）")
    rotation: int = Field(default=0, ge=-180, le=180, description="旋转角度（度）")
    margin_percent: float = Field(
        default=5.0, ge=0.0, le=50.0, description="距边缘间距（占图片短边百分比）"
    )
    font_path: str | None = Field(
        default=None, description="自定义字体文件路径，为空时使用默认字体"
    )

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        """校验颜色字符串为合法的 ``#RRGGBB`` 或 ``#RRGGBBAA`` 形式。"""
        v = value.strip()
        if not v.startswith("#"):
            raise ValueError("颜色必须是 #RRGGBB 或 #RRGGBBAA 格式")
        hex_part = v[1:]
        if len(hex_part) not in (6, 8):
            raise ValueError("颜色必须是 #RRGGBB 或 #RRGGBBAA 格式")
        try:
            int(hex_part, 16)
        except ValueError as exc:
            raise ValueError("颜色包含非法十六进制字符") from exc
        return v


class ImageWatermarkStyle(BaseModel):
    """图片水印样式（叠加 logo 或图形）。"""

    image_path: str = Field(..., min_length=1, max_length=1024, description="水印图片文件路径")
    position: WatermarkPosition = Field(
        default=WatermarkPosition.BOTTOM_RIGHT, description="水印位置"
    )
    scale: float = Field(
        default=0.1, ge=0.01, le=1.0, description="水印图片相对目标图片的缩放比例"
    )
    opacity: float = Field(default=0.7, ge=0.0, le=1.0, description="不透明度（0~1）")
    margin_percent: float = Field(
        default=5.0, ge=0.0, le=50.0, description="距边缘间距（占图片短边百分比）"
    )


class TileOptions(BaseModel):
    """平铺水印的间距配置。

    间距以目标图片尺寸的百分比动态计算，避免在不同分辨率下使用固定像素
    导致水印过密或过疏。
    """

    spacing_x_percent: float = Field(
        default=30.0, ge=5.0, le=90.0, description="水平间距（占图片宽度百分比）"
    )
    spacing_y_percent: float = Field(
        default=30.0, ge=5.0, le=90.0, description="垂直间距（占图片高度百分比）"
    )


class WatermarkTemplate(BaseModel):
    """水印模板：组合文字/图片样式与授权类型。

    通过 ``name`` 在预置注册表中查找；``license_type`` 决定版权文案主体，
    ``text`` / ``image`` 至少需提供一个，否则模板无法渲染任何可见水印。
    """

    name: str = Field(..., min_length=1, max_length=64, description="模板名称（唯一）")
    license_type: LicenseType = Field(
        default=LicenseType.COPYRIGHT, description="授权类型"
    )
    text: TextWatermarkStyle | None = Field(default=None, description="文字水印样式")
    image: ImageWatermarkStyle | None = Field(default=None, description="图片水印样式")
    tile: TileOptions | None = Field(default=None, description="平铺间距配置")

    @model_validator(mode="after")
    def _require_visible_style(self) -> "WatermarkTemplate":
        """确保模板至少包含一种可见水印样式（文字或图片）。"""
        if self.text is None and self.image is None:
            raise ValueError("模板必须包含文字水印或图片水印至少其一")
        return self


class WatermarkRequest(BaseModel):
    """批量水印应用请求。

    优先使用 ``template_name`` 解析预置模板；同时提供 ``text`` / ``image`` 时，
    与模板合并（请求中的样式覆盖模板同名样式）。``license_type`` 可覆盖模板
    默认授权类型。
    """

    source_paths: list[str] = Field(
        ..., min_length=1, max_length=200, description="源图片文件路径列表"
    )
    template_name: str | None = Field(default=None, max_length=64, description="预置模板名称")
    license_type: LicenseType | None = Field(default=None, description="授权类型（覆盖模板默认）")
    text: TextWatermarkStyle | None = Field(default=None, description="自定义文字水印样式")
    image: ImageWatermarkStyle | None = Field(default=None, description="自定义图片水印样式")
    tile: TileOptions | None = Field(default=None, description="自定义平铺间距配置")
    output_dir: str | None = Field(
        default=None, max_length=1024, description="输出目录，缺省为源文件同级目录"
    )
    overwrite: bool = Field(default=False, description="输出文件已存在时是否覆盖")

    @field_validator("source_paths")
    @classmethod
    def _validate_source_paths(cls, value: list[str]) -> list[str]:
        """去重并校验源文件路径非空、长度合法、不含非法字符。"""
        seen: list[str] = []
        for path in value:
            p = path.strip()
            if not p:
                raise ValueError("源文件路径不能为空字符串")
            if len(p) > 1024:
                raise ValueError("源文件路径长度不能超过 1024 字符")
            if any(c in p for c in ("\x00", "\n", "\r")):
                raise ValueError("源文件路径包含非法字符")
            if p not in seen:
                seen.append(p)
        return seen


class WatermarkResultItem(BaseModel):
    """单个文件的水印处理结果。"""

    source_path: str = Field(description="源文件路径")
    success: bool = Field(description="是否处理成功")
    output_path: str | None = Field(default=None, description="处理成功后的输出文件路径")
    error: str | None = Field(default=None, description="失败原因")


class WatermarkResponse(BaseModel):
    """批量水印处理统一响应。"""

    total: int = Field(ge=0, description="请求处理的文件总数")
    succeeded: int = Field(ge=0, description="成功处理数量")
    failed: int = Field(ge=0, description="失败数量")
    results: list[WatermarkResultItem] = Field(
        default_factory=list, description="逐文件结果列表"
    )
    warnings: list[str] = Field(default_factory=list, description="非致命警告信息")