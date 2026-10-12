"""元数据提取器测试：GPS DMS 转换、拍摄时间回退、EXIF 提取。"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from app.services.metadata import (
    _format_exposure,
    dms_to_decimal,
    extract_exif_metadata,
    extract_taken_at,
    parse_exif_datetime,
    MetadataExtractor,
)


# ---- dms_to_decimal ----


def test_dms_rational_tuple():
    """有理数元组转换为十进制。"""
    assert dms_to_decimal(((22, 1), (33, 1), (44, 1))) == pytest.approx(22.5622222222)


def test_dms_float_tuple():
    """小数元组转换为十进制。"""
    assert dms_to_decimal((22.0, 33.0, 44.0)) == pytest.approx(22.5622222222)


def test_dms_invalid_input():
    """非法输入返回 None。"""
    assert dms_to_decimal(None) is None
    assert dms_to_decimal(42) is None
    assert dms_to_decimal((1, 2)) is None


def test_dms_zero_denominator():
    """分母为 0 返回 None。"""
    assert dms_to_decimal(((22, 0), (33, 1), (44, 1))) is None


# ---- parse_exif_datetime ----


def test_parse_exif_datetime_str():
    """解析冒号分隔的 EXIF 时间字符串。"""
    dt = parse_exif_datetime("2023:06:15 10:00:00")
    assert dt == datetime(2023, 6, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_parse_exif_datetime_bytes():
    """解析 bytes 类型的 EXIF 时间。"""
    dt = parse_exif_datetime(b"2023:06:15 10:00:00")
    assert dt.year == 2023
    assert dt.hour == 10


def test_parse_exif_datetime_invalid():
    """非法时间返回 None。"""
    assert parse_exif_datetime("not-a-date") is None
    assert parse_exif_datetime(None) is None
    assert parse_exif_datetime(123) is None


# ---- extract_taken_at 回退 ----


def test_taken_at_prefers_datetime_original():
    """优先使用 DateTimeOriginal。"""
    exif = {"Exif": {36867: b"2023:01:01 08:00:00"}}
    dt, source = extract_taken_at(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert source == "exif_datetime_original"
    assert dt.year == 2023 and dt.hour == 8


def test_taken_at_falls_back_to_digitized():
    """无 DateTimeOriginal 时回退到 DateTimeDigitized。"""
    exif = {"Exif": {36868: b"2022:05:05 09:00:00"}}
    _, source = extract_taken_at(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert source == "exif_datetime_digitized"


def test_taken_at_falls_back_to_ifd0_datetime():
    """无 Exif 时间时回退到 0th IFD DateTime。"""
    exif = {"0th": {306: b"2021:03:03 07:00:00"}}
    _, source = extract_taken_at(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert source == "exif_datetime"


def test_taken_at_falls_back_to_file_modified():
    """无 EXIF 时间时优先文件修改时间。"""
    _, source = extract_taken_at(
        {},
        file_mtime=1700000000.0,
        file_ctime=1600000000.0,
        default_iso="1970-01-01T00:00:00+00:00",
    )
    assert source == "file_modified"


def test_taken_at_falls_back_to_file_created():
    """无修改时间时回退到文件创建时间。"""
    _, source = extract_taken_at(
        {},
        file_mtime=None,
        file_ctime=1600000000.0,
        default_iso="1970-01-01T00:00:00+00:00",
    )
    assert source == "file_created"


def test_taken_at_falls_back_to_default():
    """全部缺失时使用默认时间。"""
    dt, source = extract_taken_at({}, default_iso="1970-01-01T00:00:00+00:00")
    assert source == "default"
    assert dt == datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---- EXIF 元数据提取 ----


def test_extract_exif_metadata_make_model():
    """提取相机厂商与型号（bytes）。"""
    exif = {"0th": {271: b"Canon", 272: b"EOS 5D", 306: b"2023:06:15 10:00:00"}}
    meta = extract_exif_metadata(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert meta.make == "Canon"
    assert meta.model == "EOS 5D"


def test_extract_gps_north_east_positive():
    """北纬东经为正数。"""
    exif = {
        "GPS": {
            1: b"N",
            2: ((33, 1), (52, 1), (4, 1)),
            3: b"E",
            4: ((151, 1), (12, 1), (24, 1)),
        }
    }
    meta = extract_exif_metadata(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert meta.gps is not None
    assert meta.gps.latitude == pytest.approx(33 + 52 / 60 + 4 / 3600)
    assert meta.gps.longitude == pytest.approx(151 + 12 / 60 + 24 / 3600)


def test_extract_gps_south_west_negative():
    """南纬西经为负数。"""
    exif = {
        "GPS": {
            1: b"S",
            2: ((33, 1), (52, 1), (4, 1)),
            3: b"W",
            4: ((151, 1), (12, 1), (24, 1)),
        }
    }
    meta = extract_exif_metadata(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert meta.gps is not None
    assert meta.gps.latitude < 0
    assert meta.gps.longitude < 0
    assert meta.gps.latitude == pytest.approx(-(33 + 52 / 60 + 4 / 3600))
    assert meta.gps.longitude == pytest.approx(-(151 + 12 / 60 + 24 / 3600))


def test_extract_no_gps():
    """无 GPS 时 gps 字段为 None。"""
    exif = {"Exif": {36867: b"2023:06:15 10:00:00"}}
    meta = extract_exif_metadata(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert meta.gps is None


def test_extract_iso_exposure_fnumber():
    """提取感光度/曝光时间/光圈。"""
    exif = {
        "Exif": {
            36867: b"2023:06:15 10:00:00",
            34855: 400,
            33434: (1, 60),
            33437: (28, 10),
        }
    }
    meta = extract_exif_metadata(exif, default_iso="1970-01-01T00:00:00+00:00")
    assert meta.iso == 400
    assert meta.exposure_time == "1/60"
    assert meta.f_number == pytest.approx(2.8)


# ---- _format_exposure ----


def test_format_exposure_fraction():
    """小于 1 秒的曝光时间格式化为分数。"""
    assert _format_exposure((1, 60)) == "1/60"


def test_format_exposure_one_or_more():
    """大于等于 1 秒显示为小数。"""
    assert _format_exposure((1, 1)) == "1"


# ---- MetadataExtractor.extract ----


def test_extractor_file_time_fallback(tmp_path):
    """piexif 不可用时回退到文件修改时间。"""
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"fake-image-bytes")
    mtime = 1700000000.0
    os.utime(str(f), (mtime, mtime))

    extractor = MetadataExtractor("1970-01-01T00:00:00+00:00")
    meta = extractor.extract(str(f))
    assert meta.taken_at_source == "file_modified"
    assert meta.taken_at is not None


def test_extractor_file_not_found(tmp_path):
    """文件不存在时抛出 FileNotFoundError。"""
    extractor = MetadataExtractor("1970-01-01T00:00:00+00:00")
    with pytest.raises(FileNotFoundError):
        extractor.extract(str(tmp_path / "nonexistent.jpg"))