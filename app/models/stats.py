"""统计接口响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MapPoint(BaseModel):
    """地图足迹点（按坐标去重聚合后）。"""

    lat: float = Field(description="纬度")
    lng: float = Field(description="经度")
    count: int = Field(ge=0, description="共享该坐标的资产数")
    asset_ids: list[str] | None = Field(
        default=None, description="共享该坐标的资产 ID；show_ids=false 时为 null"
    )


class CityStat(BaseModel):
    """城市聚合统计。"""

    city: str = Field(description="城市名（未知时为空串）")
    country: str = Field(description="国家名（未知时为空串）")
    count: int = Field(ge=0, description="该城市的资产数")
    lat: float = Field(description="代表坐标纬度")
    lng: float = Field(description="代表坐标经度")


class CountryStat(BaseModel):
    """国家聚合统计。"""

    country: str = Field(description="国家名")
    count: int = Field(ge=0, description="该国家的资产数")


class MapStatsResponse(BaseModel):
    """地图足迹统计响应。

    返回带 GPS 的照片点、城市聚合、国家聚合。
    无 GPS 资产不计入任何字段；上游不可用时降级返回空结果（degraded=true），状态码仍为 200。
    """

    total_with_gps: int = Field(ge=0, description="带有效 GPS 的资产总数")
    degraded: bool = Field(default=False, description="是否发生降级（上游不可用等）")
    warnings: list[str] = Field(default_factory=list, description="降级/告警信息")
    points: list[MapPoint] = Field(default_factory=list, description="原始 GPS 点（cluster=raw 时，最多 2000 条）")
    cities: list[CityStat] = Field(default_factory=list, description="城市聚合（cluster=city 时）")
    countries: list[CountryStat] = Field(default_factory=list, description="国家聚合")


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
