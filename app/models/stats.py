"""统计接口响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class NameCount(BaseModel):
    """名称-计数对（用于相机品牌、相机型号、镜头等）。"""

    name: str
    count: int = Field(ge=0)


class ValueCount(BaseModel):
    """值-计数对（用于光圈、ISO、焦距、快门速度等分布）。"""

    value: str
    count: int = Field(ge=0)


class ExifStatsResponse(BaseModel):
    """EXIF 统计分布响应。

    聚合图库资产的 EXIF 元数据分布，用于可视化大屏。
    上游不可用时降级返回空分布（degraded=true），状态码仍为 200。
    """

    total: int = Field(ge=0, description="处理的资产总数")
    degraded: bool = Field(default=False, description="是否发生降级（上游不可用等）")
    warnings: list[str] = Field(default_factory=list, description="降级/告警信息")
    camera_brands: list[NameCount] = Field(default_factory=list, description="相机品牌分布（top 20）")
    cameras: list[NameCount] = Field(default_factory=list, description="相机型号分布（top 20）")
    lenses: list[NameCount] = Field(default_factory=list, description="镜头型号分布（top 20）")
    aperture_distribution: list[ValueCount] = Field(default_factory=list, description="光圈值分布（top 20）")
    iso_distribution: list[ValueCount] = Field(default_factory=list, description="ISO 分布（top 20）")
    focal_length_distribution: list[ValueCount] = Field(default_factory=list, description="焦距分布（top 20）")
    shutter_speed_distribution: list[ValueCount] = Field(default_factory=list, description="快门速度分布（top 20）")