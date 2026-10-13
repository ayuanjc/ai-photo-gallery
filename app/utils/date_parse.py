"""日期参数解析工具：将 ISO 8601 字符串解析为 timezone-aware datetime。"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_date_param(value: str | None, *, field_name: str = "date") -> datetime | None:
    """将 ISO 8601 字符串解析为 timezone-aware datetime。

    支持：
    - None → None
    - 带时区（如 2024-01-01T00:00:00+08:00）
    - Z 后缀（如 2024-01-01T00:00:00Z）
    - 无时区（视为 UTC）

    异常:
        ValueError: 无法解析时抛出，消息包含字段名。
    """
    if value is None or value.strip() == "":
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} 格式无效，需 ISO 8601: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt