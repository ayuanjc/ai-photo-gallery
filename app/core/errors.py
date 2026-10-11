"""统一的错误码体系与领域异常。

对外接口返回统一错误结构：
    { "error": { "code": "...", "message": "...", "request_id": "...", "details": ... } }

领域异常（AppError）在 service 层抛出，由全局异常处理器转换为 HTTP 响应。
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    # 请求参数校验失败（FastAPI/Pydantic 校验错误统一映射）
    INVALID_PARAMS = "INVALID_PARAMS"
    # 上游 Immich 不可用（连接失败、5xx）
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    # 上游 Immich 请求超时
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    # 上游 Immich 返回异常（4xx、协议错误等）
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    # 华为云视觉服务不可用（连接失败、5xx）
    VISION_UNAVAILABLE = "VISION_UNAVAILABLE"
    # 华为云视觉服务请求超时
    VISION_TIMEOUT = "VISION_TIMEOUT"
    # 华为云视觉服务返回异常（4xx、鉴权失败、协议错误等）
    VISION_ERROR = "VISION_ERROR"
    # 请求未授权（缺 API Key 或 Key 不正确）
    UNAUTHORIZED = "UNAUTHORIZED"
    # AI 分析器上游不可用（连接失败、5xx）
    AI_ANALYZER_UNAVAILABLE = "AI_ANALYZER_UNAVAILABLE"
    # AI 分析器上游请求超时
    AI_ANALYZER_TIMEOUT = "AI_ANALYZER_TIMEOUT"
    # 服务内部错误
    INTERNAL_ERROR = "INTERNAL_ERROR"


# 错误码 -> HTTP 状态码
STATUS_CODE_MAP: dict[ErrorCode, int] = {
    ErrorCode.INVALID_PARAMS: 400,
    ErrorCode.UPSTREAM_UNAVAILABLE: 502,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.VISION_UNAVAILABLE: 502,
    ErrorCode.VISION_TIMEOUT: 504,
    ErrorCode.VISION_ERROR: 502,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.AI_ANALYZER_UNAVAILABLE: 502,
    ErrorCode.AI_ANALYZER_TIMEOUT: 504,
    ErrorCode.INTERNAL_ERROR: 500,
}


class AppError(Exception):
    """服务层抛出的领域异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def status_code(self) -> int:
        return STATUS_CODE_MAP[self.code]


class UpstreamUnavailableError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.UPSTREAM_UNAVAILABLE, message, details)


class UpstreamTimeoutError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.UPSTREAM_TIMEOUT, message, details)


class UpstreamError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.UPSTREAM_ERROR, message, details)

class VisionUnavailableError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.VISION_UNAVAILABLE, message, details)


class VisionTimeoutError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.VISION_TIMEOUT, message, details)


class VisionError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.VISION_ERROR, message, details)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "API Key 缺失或不正确", details: Any = None) -> None:
        super().__init__(ErrorCode.UNAUTHORIZED, message, details)


class AIAnalyzerUnavailableError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.AI_ANALYZER_UNAVAILABLE, message, details)


class AIAnalyzerTimeoutError(AppError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.AI_ANALYZER_TIMEOUT, message, details)
