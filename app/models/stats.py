"""统计接口响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class GearTopItem(BaseModel):
    """器材 Top 项（相机 / 镜头通用）。"""

    name: str = Field(description="名称")
    count: int = Field(ge=0, description="数量")
    ratio: float = Field(ge=0.0, le=1.0, description="占比（count / total，保留 2 位小数）")


class FavoriteCombo(BaseModel):
    """最常用相机 + 镜头组合。"""

    camera: str = Field(description="相机名称")
    lens: str = Field(description="镜头名称")
    count: int = Field(ge=0, description="该组合出现次数")


class BrandRatioItem(BaseModel):
    """品牌占比项。"""

    brand: str = Field(description="品牌名（make）")
    count: int = Field(ge=0, description="数量")
    ratio: float = Field(ge=0.0, le=1.0, description="占比（count / total，保留 2 位小数）")


class MonthCount(BaseModel):
    """月度拍摄计数。"""

    month: str = Field(description="月份（YYYY-MM）")
    count: int = Field(ge=0, description="该月拍摄数量")


class GearStatsResponse(BaseModel):
    """器材偏好统计响应。

    返回常用相机、镜头、组合、品牌占比、近 12 个月月度分布。
    ratio = count / total，保留 2 位小数；无器材信息的资产不计入 top 系列但计入 total。
    上游不可用时降级返回空结果（degraded=true），状态码仍为 200。
    """

    total: int = Field(ge=0, description="处理的资产总数")
    degraded: bool = Field(default=False, description="是否发生降级（上游不可用等）")
    warnings: list[str] = Field(default_factory=list, description="降级/告警信息")
    top_cameras: list[GearTopItem] = Field(default_factory=list, description="常用相机（top 10）")
    top_lenses: list[GearTopItem] = Field(default_factory=list, description="常用镜头（top 10）")
    favorite_combo: FavoriteCombo | None = Field(default=None, description="最常用相机+镜头组合（无则 null）")
    brand_ratio: list[BrandRatioItem] = Field(default_factory=list, description="品牌占比")
    shot_by_month: list[MonthCount] = Field(default_factory=list, description="近 12 个月月度分布，按月份升序")