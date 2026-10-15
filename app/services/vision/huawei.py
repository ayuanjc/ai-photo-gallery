"""华为云图像识别（Image Tagging）适配器。

通过 AK/SK 签名调用华为云 Image 服务 REST API：
    POST /v1.0/image/tagging — 对图片 URL 执行标签识别

签名由 HuaweiCloudSigner 完成，无需引入官方 SDK。
错误分类：连接失败 -> VISION_UNAVAILABLE，超时 -> VISION_TIMEOUT，
其他 4xx/协议异常 -> VISION_ERROR。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ...core.config import Settings
from ...core.errors import VisionError, VisionTimeoutError, VisionUnavailableError
from .base import VisionProvider, VisionResult
from .huawei_signer import HuaweiCloudSigner

logger = logging.getLogger(__name__)


class HuaweiVisionProvider(VisionProvider):
    name = "huawei"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._signer = HuaweiCloudSigner(
            settings.huawei_ak, settings.huawei_sk.get_secret_value()
        )
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            timeout = httpx.Timeout(
                connect=settings.huawei_image_connect_timeout_seconds,
                read=settings.huawei_image_timeout_seconds,
                write=settings.huawei_image_timeout_seconds,
                pool=settings.huawei_image_timeout_seconds,
            )
            self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)
            self._owns_client = True

    @property
    def endpoint(self) -> str:
        return self._settings.huawei_image_endpoint.rstrip("/")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def analyze(self, image_url: str) -> VisionResult:
        if not self._settings.huawei_ak or not self._settings.huawei_sk.get_secret_value():
            raise VisionUnavailableError(
                "华为云图像识别未配置 AK/SK，请在配置中设置 HUAWEI_AK / HUAWEI_SK"
            )

        path = "/v1.0/image/tagging"
        url = f"{self.endpoint}{path}"
        body = json.dumps({"image_url": image_url}, separators=(",", ":")).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}

        signed_headers = self._signer.sign("POST", url, headers, body=body)
        try:
            resp = await self._client.request(
                "POST", url, headers=signed_headers, content=body
            )
        except httpx.TimeoutException as exc:
            raise VisionTimeoutError(
                f"Image Tagging 请求超时: {path}", details=str(exc)
            ) from exc
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
            raise VisionUnavailableError(
                f"Image Tagging 服务不可用: {path}", details=str(exc)
            ) from exc

        if resp.status_code >= 500:
            logger.debug("Image Tagging 5xx body: %s", resp.text[:500])
            raise VisionUnavailableError(
                f"Image Tagging 返回 {resp.status_code}",
                details={"status": resp.status_code},
            )
        if resp.status_code in (401, 403):
            raise VisionError(
                "Image Tagging 鉴权失败：AK/SK 无效",
                details={"status": resp.status_code},
            )
        if resp.status_code >= 400:
            logger.debug("Image Tagging 4xx body: %s", resp.text[:500])
            raise VisionError(
                f"Image Tagging 返回异常状态码 {resp.status_code}",
                details={"status": resp.status_code},
            )

        data = self._parse_json(resp)
        tags = [t for t in data.get("tags", []) if isinstance(t, dict)]
        labels = [str(t["tag"]) for t in tags if t.get("tag")]
        return VisionResult(labels=labels, tags=labels, raw=data)

    @staticmethod
    def _parse_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("Image Tagging 非 JSON body: %s", resp.text[:500])
            raise VisionError(
                "Image Tagging 返回非 JSON 响应",
                details={"status": resp.status_code},
            ) from exc
        if not isinstance(data, dict):
            raise VisionError(
                "Image Tagging 返回非对象 JSON",
                details={"status": resp.status_code},
            )
        return data
