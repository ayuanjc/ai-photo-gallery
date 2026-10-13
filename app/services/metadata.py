"""基于 piexif 的图片元数据提取器。

从本地图片文件提取 EXIF 元数据，重点处理：
- 拍摄时间（DateTimeOriginal）的多级回退，确保 taken_at 始终有值。
- GPS 坐标从 DMS（度分秒）到十进制（Decimal Degrees）的转换，南纬/西经为负数。
- piexif 不可用或 EXIF 解析失败时优雅降级（仅用文件时间回退）。

纯函数（dms_to_decimal / parse_exif_datetime / extract_taken_at 等）与
piexif 解耦，便于单元测试。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from ..models.batch_import import ExifMetadata, GpsInfo, TakenAtSource

try:
    import piexif

    _HAS_PIEXIF = True
except ImportError:  # pragma: no cover - 运行环境缺少 piexif 时降级
    piexif = None
    _HAS_PIEXIF = False

logger = logging.getLogger(__name__)

# 标准 EXIF tag 编号（与 piexif 解耦，仅依赖其返回的 dict 结构）。
IFD0_TAG_DATETIME = 306
IFD0_TAG_MAKE = 271
IFD0_TAG_MODEL = 272
EXIF_TAG_DATETIME_ORIGINAL = 36867
EXIF_TAG_DATETIME_DIGITIZED = 36868
EXIF_TAG_ISO = 34855
EXIF_TAG_EXPOSURE_TIME = 33434
EXIF_TAG_FNUMBER = 33437
GPS_TAG_LAT_REF = 1
GPS_TAG_LAT = 2
GPS_TAG_LON_REF = 3
GPS_TAG_LON = 4


def _to_float(component) -> float | None:
    """将 DMS 分量（有理数 (num, den) 元组或小数）转换为浮点数。

    参数:
        component: EXIF 中的数值分量，可能为 (num, den) 二元组或标量。

    返回:
        转换后的浮点数；无法转换或分母为 0 时返回 None。
    """
    if isinstance(component, (tuple, list)) and len(component) == 2:
        num, den = component
        try:
            den_f = float(den)
            if den_f == 0:
                return None
            return float(num) / den_f
        except (TypeError, ValueError):
            return None
    try:
        return float(component)
    except (TypeError, ValueError):
        return None


def dms_to_decimal(dms) -> float | None:
    """将度分秒（DMS）三元组转换为十进制角度。

    参数:
        dms: (度, 分, 秒) 三元组，每项可为 (num, den) 有理数元组或小数。

    返回:
        十进制角度；输入非法时返回 None。

    示例:
        dms_to_decimal(((22, 1), (33, 1), (44, 1))) -> 22.5622...
    """
    if not isinstance(dms, (tuple, list)) or len(dms) != 3:
        return None
    degrees = _to_float(dms[0])
    minutes = _to_float(dms[1])
    seconds = _to_float(dms[2])
    if degrees is None or minutes is None or seconds is None:
        return None
    return degrees + minutes / 60.0 + seconds / 3600.0


def _ref_value(value) -> str:
    """将 GPS 参考值（N/S/E/W，可能为 bytes）转换为大写字符串。"""
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore").strip().upper()
    if isinstance(value, str):
        return value.strip().upper()
    return ""


def _str_value(value) -> str | None:
    """将 EXIF 字符串值（可能为 bytes）转换为去空白后的字符串。"""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    text = str(value).strip()
    return text or None


def parse_exif_datetime(value) -> datetime | None:
    """解析 EXIF 时间字符串为带 UTC 时区的 datetime。

    参数:
        value: EXIF 时间值，形如 "YYYY:MM:DD HH:MM:SS"（可能为 bytes）。

    返回:
        aware datetime（UTC）；无法解析时返回 None。
    """
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    if not isinstance(value, str):
        return None
    text = value.strip().strip("\x00").strip()
    if not text:
        return None
    formats = (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M",
        "%Y-%m-%d %H:%M",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_default_iso(iso: str) -> datetime:
    """解析默认时间字符串，失败时回退到 Unix 纪元。"""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def extract_taken_at(
    exif_dict: dict,
    *,
    file_mtime: float | None = None,
    file_ctime: float | None = None,
    default_iso: str = "1970-01-01T00:00:00+00:00",
) -> tuple[datetime, TakenAtSource]:
    """按优先级回退获取拍摄时间，确保始终有值。

    回退顺序:
        1. EXIF DateTimeOriginal
        2. EXIF DateTimeDigitized
        3. 0th IFD DateTime
        4. 文件修改时间
        5. 文件创建时间
        6. 全局默认时间

    返回:
        (拍摄时间, 来源) 元组。
    """
    exif_ifd = (exif_dict or {}).get("Exif", {}) or {}
    ifd0 = (exif_dict or {}).get("0th", {}) or {}

    for tag, source in (
        (EXIF_TAG_DATETIME_ORIGINAL, "exif_datetime_original"),
        (EXIF_TAG_DATETIME_DIGITIZED, "exif_datetime_digitized"),
    ):
        dt = parse_exif_datetime(exif_ifd.get(tag))
        if dt is not None:
            return dt, source  # type: ignore[return-value]

    dt = parse_exif_datetime(ifd0.get(IFD0_TAG_DATETIME))
    if dt is not None:
        return dt, "exif_datetime"

    if file_mtime is not None:
        return datetime.fromtimestamp(file_mtime, tz=timezone.utc), "file_modified"
    if file_ctime is not None:
        return datetime.fromtimestamp(file_ctime, tz=timezone.utc), "file_created"
    return _parse_default_iso(default_iso), "default"


def _extract_gps_raw(exif_dict: dict) -> tuple[float | None, float | None]:
    """提取 GPS 原始坐标并完成 DMS→十进制转换，南纬/西经为负数。"""
    gps = (exif_dict or {}).get("GPS", {}) or {}
    if not gps:
        return None, None

    lat = dms_to_decimal(gps.get(GPS_TAG_LAT))
    lon = dms_to_decimal(gps.get(GPS_TAG_LON))
    lat_ref = _ref_value(gps.get(GPS_TAG_LAT_REF))
    lon_ref = _ref_value(gps.get(GPS_TAG_LON_REF))

    if lat is not None and lat_ref == "S":
        lat = -abs(lat)
    if lon is not None and lon_ref == "W":
        lon = -abs(lon)

    # 越界值视为无效，避免下游模型校验失败。
    if lat is not None and not (-90.0 <= lat <= 90.0):
        lat = None
    if lon is not None and not (-180.0 <= lon <= 180.0):
        lon = None
    return lat, lon


def _format_exposure(exposure) -> str | None:
    """格式化曝光时间：短于 1 秒显示为分数（如 1/60），否则为小数。"""
    if exposure is None:
        return None
    if isinstance(exposure, (tuple, list)) and len(exposure) == 2:
        num = _to_float(exposure[0])
        den = _to_float(exposure[1])
        if num is not None and den is not None and den != 0:
            value = num / den
            if 0 < value < 1:
                rounded = round(den / num)
                return f"1/{rounded}"
            return f"{value:g}"
    value = _to_float(exposure)
    return f"{value:g}" if value is not None else None


def _extract_fnumber(fnumber) -> float | None:
    """提取光圈值（可能为 (num, den) 分数或小数）。"""
    value = _to_float(fnumber)
    return value if value is not None and value > 0 else None


def extract_exif_metadata(
    exif_dict: dict,
    *,
    file_mtime: float | None = None,
    file_ctime: float | None = None,
    default_iso: str = "1970-01-01T00:00:00+00:00",
) -> ExifMetadata:
    """从 EXIF dict 提取完整元数据。

    参数:
        exif_dict: piexif.load 返回的 dict（含 "0th"/"Exif"/"GPS" 键）。
        file_mtime: 文件修改时间戳（可选，用于拍摄时间回退）。
        file_ctime: 文件创建时间戳（可选，用于拍摄时间回退）。
        default_iso: 全局默认时间 ISO 字符串。

    返回:
        ExifMetadata 模型，taken_at 始终有值。
    """
    taken_at, source = extract_taken_at(
        exif_dict,
        file_mtime=file_mtime,
        file_ctime=file_ctime,
        default_iso=default_iso,
    )

    exif_ifd = (exif_dict or {}).get("Exif", {}) or {}
    ifd0 = (exif_dict or {}).get("0th", {}) or {}

    iso_raw = exif_ifd.get(EXIF_TAG_ISO)
    iso_value = _to_float(iso_raw)
    iso: int | None = int(iso_value) if iso_value is not None and iso_value >= 0 else None

    lat, lon = _extract_gps_raw(exif_dict)
    gps: GpsInfo | None = None
    if lat is not None or lon is not None:
        gps = GpsInfo(latitude=lat, longitude=lon)

    return ExifMetadata(
        taken_at=taken_at,
        taken_at_source=source,
        make=_str_value(ifd0.get(IFD0_TAG_MAKE)),
        model=_str_value(ifd0.get(IFD0_TAG_MODEL)),
        iso=iso,
        exposure_time=_format_exposure(exif_ifd.get(EXIF_TAG_EXPOSURE_TIME)),
        f_number=_extract_fnumber(exif_ifd.get(EXIF_TAG_FNUMBER)),
        gps=gps,
    )


class MetadataExtractor:
    """从本地文件提取 EXIF 元数据（带 piexif 降级与时间回退）。"""

    def __init__(self, default_datetime_iso: str = "1970-01-01T00:00:00+00:00") -> None:
        self._default_iso = default_datetime_iso

    def extract(self, file_path: str) -> ExifMetadata:
        """提取单个文件的元数据。

        参数:
            file_path: 本地图片文件路径。

        返回:
            ExifMetadata（taken_at 始终有值）。

        异常:
            FileNotFoundError: 文件不存在时抛出（由调用方决定是否降级）。
        """
        st = os.stat(file_path)

        exif_dict: dict = {}
        if _HAS_PIEXIF:
            try:
                exif_dict = piexif.load(file_path) or {}
            except Exception as exc:  # noqa: BLE001 - EXIF 解析失败仅降级
                logger.debug("EXIF 解析失败 file=%s err=%s", file_path, exc)
                exif_dict = {}

        return extract_exif_metadata(
            exif_dict,
            file_mtime=st.st_mtime,
            file_ctime=st.st_ctime,
            default_iso=self._default_iso,
        )