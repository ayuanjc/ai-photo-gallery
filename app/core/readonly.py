"""比赛/演示只读闸：归档写操作在只读模式下直接拒绝。

只拦「写图库」的接口（archive propose 里的写、commit、rollback），
搜索与素材包属于读操作，不受影响。
"""
from __future__ import annotations

from ..core.config import Settings
from ..core.errors import AppError, ErrorCode


def read_only_error(message: str = "") -> AppError:
    return AppError(
        ErrorCode.FORBIDDEN,
        message or "当前为比赛演示只读模式（COMPETITION_READONLY=true），归档写操作已关闭",
        details={"hint": "如需写回图库，先设置 COMPETITION_READONLY=false 或走 POST /archive/propose 走人工确认"},
    )


def ensure_writable(settings: Settings) -> None:
    """只读模式下抛 403。"""
    if bool(getattr(settings, "competition_readonly", False)):
        raise read_only_error()


def is_readonly(settings: Settings) -> bool:
    return bool(getattr(settings, "competition_readonly", False))
