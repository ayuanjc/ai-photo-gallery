"""自然语言时间解析器：从查询词中识别时间表达，转成日期范围。

支持的时间表达（第一版）：
    - 相对时间：今天 / 昨天 / 前天 / 本周 / 上周 / 本月 / 上月 / 今年 / 去年
    - 季度+年份：今年夏天 / 去年冬天 / 前年春天 / 去年秋天 …
      （春=3-5月，夏=6-8月，秋=9-11月，冬=12-2月）
    - 模糊：最近（近 7 天）/ 最近一周 / 最近一个月 / 最近一年
    - TODO（第一版不做）：生日 / 春节 / 国庆

设计原则：
    - 纯标准库（datetime + calendar），不引新依赖。
    - 无法识别的时间表达：忽略，不报错，query 原样保留。
    - 识别到时间后，从 query 里移除该表达，避免干扰语义搜索。
    - now 参数用于测试注入。
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# 季节 → (起始月, 结束月)
_SEASON_MONTHS: dict[str, tuple[int, int]] = {
    "春天": (3, 5),
    "春": (3, 5),
    "夏天": (6, 8),
    "夏": (6, 8),
    "秋天": (9, 11),
    "秋": (9, 11),
    "冬天": (12, 2),
    "冬": (12, 2),
}

# 年份修饰词 → 相对偏移量（0=今年, -1=去年, -2=前年）
_YEAR_OFFSETS: dict[str, int] = {
    "今年": 0,
    "去年": -1,
    "前年": -2,
}


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)


def _start_of_month(year: int, month: int) -> datetime:
    return datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)


def _end_of_month(year: int, month: int) -> datetime:
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)


def _resolve_relative(pattern: str, now: datetime) -> tuple[datetime, datetime] | None:
    today = _start_of_day(now)

    if pattern == "今天":
        return today, _end_of_day(now)
    if pattern == "昨天":
        d = today - timedelta(days=1)
        return d, _end_of_day(d)
    if pattern == "前天":
        d = today - timedelta(days=2)
        return d, _end_of_day(d)
    if pattern == "本周":
        start = today - timedelta(days=today.weekday())
        return start, _end_of_day(start + timedelta(days=6))
    if pattern == "上周":
        this_week_start = today - timedelta(days=today.weekday())
        start = this_week_start - timedelta(days=7)
        return start, _end_of_day(start + timedelta(days=6))
    if pattern == "本月":
        return _start_of_month(now.year, now.month), _end_of_month(now.year, now.month)
    if pattern == "上月" or pattern == "上个月":
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        return _start_of_month(year, month), _end_of_month(year, month)
    if pattern == "今年":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc), now
    if pattern == "去年":
        return datetime(now.year - 1, 1, 1, tzinfo=timezone.utc), datetime(
            now.year - 1, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc
        )
    return None


def _resolve_season(pattern: str, now: datetime) -> tuple[datetime, datetime] | None:
    for year_word, offset in _YEAR_OFFSETS.items():
        for season_word, (m_start, m_end) in _SEASON_MONTHS.items():
            combo = f"{year_word}{season_word}"
            if pattern == combo:
                target_year = now.year + offset
                if m_start <= m_end:
                    return (
                        _start_of_month(target_year, m_start),
                        _end_of_month(target_year, m_end),
                    )
                else:
                    return (
                        _start_of_month(target_year, m_start),
                        _end_of_month(target_year + 1, m_end),
                    )
    return None


def _resolve_fuzzy(pattern: str, now: datetime) -> tuple[datetime, datetime] | None:
    if pattern == "最近一周" or pattern == "最近":
        return now - timedelta(days=7), now
    if pattern == "最近一个月":
        return now - timedelta(days=30), now
    if pattern == "最近一年":
        return now - timedelta(days=365), now
    return None


def _build_all_patterns() -> list[str]:
    patterns: set[str] = set()

    patterns.update(
        ["今天", "昨天", "前天", "本周", "上周", "本月", "上月", "上个月", "今年", "去年"]
    )

    for year_word in _YEAR_OFFSETS:
        for season_word in _SEASON_MONTHS:
            patterns.add(f"{year_word}{season_word}")

    patterns.update(["最近一周", "最近", "最近一个月", "最近一年"])

    return sorted(patterns, key=len, reverse=True)


_ALL_PATTERNS: list[str] = _build_all_patterns()


def _resolve_pattern(pattern: str, now: datetime) -> tuple[datetime, datetime] | None:
    result = _resolve_relative(pattern, now)
    if result is not None:
        return result
    result = _resolve_season(pattern, now)
    if result is not None:
        return result
    result = _resolve_fuzzy(pattern, now)
    return result


def parse_time_hint(
    query: str, now: datetime | None = None
) -> tuple[str, tuple[datetime, datetime] | None]:
    """从查询词中识别自然语言时间表达，转成日期范围。

    Args:
        query: 原始查询词，可能包含时间表达。
        now: 当前时间，用于测试注入。为 None 时用 datetime.now(UTC)。

    Returns:
        (cleaned_query, time_range)
        - cleaned_query: 移除时间表达后的查询词。若移除后为空则保留原 query。
        - time_range: (date_from, date_to) 或 None。无法识别时为 None。
    """
    if not query or not query.strip():
        return query, None

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    best_match: str | None = None
    best_pos: int = -1
    for pattern in _ALL_PATTERNS:
        pos = query.find(pattern)
        if pos != -1:
            if best_match is None or pos < best_pos or (
                pos == best_pos and len(pattern) > len(best_match)
            ):
                best_match = pattern
                best_pos = pos

    if best_match is None:
        return query, None

    try:
        time_range = _resolve_pattern(best_match, now)
    except Exception as exc:
        logger.warning("时间解析异常 pattern=%s: %s", best_match, exc)
        return query, None

    if time_range is None:
        return query, None

    cleaned = query[:best_pos] + query[best_pos + len(best_match):]
    cleaned = cleaned.strip()
    if cleaned.startswith("的"):
        cleaned = cleaned[1:].strip()
    if not cleaned:
        cleaned = query

    return cleaned, time_range


def filter_by_time_range(
    assets: list[dict], date_from: datetime, date_to: datetime, default_iso: str
) -> list[dict]:
    """按拍摄时间范围过滤 asset 列表。

    Args:
        assets: asset 列表。
        date_from: 起始时间（含）。
        date_to: 结束时间（含）。
        default_iso: 默认时间 ISO 字符串（用于无法解析时间的 asset）。

    Returns:
        过滤后的 asset 列表（保留原顺序）。
    """
    from .ranking import resolve_captured_at

    result: list[dict] = []
    for asset in assets:
        try:
            captured_at, _ = resolve_captured_at(asset, default_iso)
        except Exception as exc:
            logger.warning("时间过滤解析拍摄时间失败 asset_id=%s: %s", asset.get("id"), exc)
            continue
        if captured_at is None:
            continue
        if date_from <= captured_at <= date_to:
            result.append(asset)
    return result
