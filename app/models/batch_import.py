"""批量导入元数据的请求/响应模型与 EXIF/GPS 子模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 拍摄时间来源枚举：按回退优先级排列。
TakenAtSource = Literal[
    "exif_datetime_original",  # EXIF DateTimeOriginal
    "exif_datetime_digitized",  # EXIF DateTimeDigitized
    "exif_datetime",  # 0th IFD DateTime
    "file_modified",  # 文件修改时间
    "file_created",  # 文件创建时间
    "default",  # 全局默认时间
]


class GpsInfo(BaseModel):
    """GPS 坐标（已从 DMS 度分秒转换为十进制，南纬/西经为负数）。"""

    latitude: float | None = Field(default=None, ge=-90.0, le=90.0, description="纬度（十进制）")
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0, description="经度（十进制）")

    def is_valid(self) -> bool:
        """判断是否存在有效坐标。"""
        return self.latitude is not None and self.longitude is not None


class ExifMetadata(BaseModel):
    """单张图片提取出的 EXIF 元数据（含回退后的拍摄时间）。"""

    taken_at: datetime = Field(description="拍摄时间（经回退策略保证始终有值）")
    taken_at_source: TakenAtSource = Field(description="拍摄时间来源")
    make: str | None = Field(default=None, description="相机厂商")
    model: str | None = Field(default=None, description="相机型号")
    iso: int | None = Field(default=None, ge=0, description="感光度 ISO")
    exposure_time: str | None = Field(default=None, description="曝光时间（形如 1/60）")
    f_number: float | None = Field(default=None, ge=0.0, description="光圈值")
    gps: GpsInfo | None = Field(default=None, description="GPS 坐标")


class ImportRequest(BaseModel):
    """批量导入请求。

    - file_paths: 服务器本地图片文件路径列表（必填、非空、去重）。
    - device_id: Immich 设备 ID（可选，缺省时使用全局配置）。
    - is_favorite: 导入后是否标记为收藏。
    """

    file_paths: list[str] = Field(..., min_length=1, max_length=200, description="本地文件路径列表")
    device_id: str | None = Field(default=None, max_length=128, description="Immich 设备 ID")
    is_favorite: bool = Field(default=False, description="是否标记为收藏")

    @field_validator("file_paths")
    @classmethod
    def _validate_file_paths(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for path in value:
            p = path.strip()
            if not p:
                raise ValueError("文件路径不能为空字符串")
            if len(p) > 1024:
                raise ValueError("文件路径长度不能超过 1024 字符")
            if any(c in p for c in ("\x00", "\n", "\r")):
                raise ValueError("文件路径包含非法字符")
            if p not in seen:
                seen.append(p)
        return seen


class ImportResultItem(BaseModel):
    """单个文件的导入结果。"""

    file_path: str = Field(description="源文件路径")
    success: bool = Field(description="是否导入成功")
    asset_id: str | None = Field(default=None, description="导入成功后的 Immich 资产 ID")
    error: str | None = Field(default=None, description="失败原因")
    metadata: ExifMetadata | None = Field(default=None, description="提取到的 EXIF 元数据")


class ImportResponse(BaseModel):
    """批量导入统一响应。"""

    total: int = Field(ge=0, description="请求处理的文件总数")
    succeeded: int = Field(ge=0, description="成功导入数量")
    failed: int = Field(ge=0, description="失败数量")
    results: list[ImportResultItem] = Field(default_factory=list, description="逐文件结果列表")
    warnings: list[str] = Field(default_factory=list, description="非致命警告信息")
