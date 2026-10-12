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