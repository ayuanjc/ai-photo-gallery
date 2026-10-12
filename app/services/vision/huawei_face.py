"""华为云人脸识别服务（FRS）异步客户端。

封装华为云 FRS REST API，通过 AK/SK 签名调用：
    - 人脸检测: POST /v2/{project_id}/face-detect
    - 创建人脸库: POST /v2/{project_id}/face-sets
    - 添加人脸: POST /v2/{project_id}/face-sets/{name}/faces
    - 搜索人脸: POST /v2/{project_id}/face-sets/{name}/search
    - 删除人脸库: DELETE /v2/{project_id}/face-sets/{name}

特性：
- 异步（httpx.AsyncClient 复用连接池）。
- 请求签名由 HuaweiCloudSigner 完成，无需引入官方 SDK。
- 错误分类：连接失败 -> VISION_UNAVAILABLE，超时 -> VISION_TIMEOUT，
  其他 4xx/协议异常 -> VISION_ERROR。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from ...core.config import Settings
from ...core.errors import VisionError, VisionTimeoutError, VisionUnavailableError
from .huawei_signer import HuaweiCloudSigner

logger = logging.getLogger(__name__)


class HuaweiFaceClient:
    """华为云 FRS 人脸识别异步客户端。"""

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
                connect=settings.huawei_face_connect_timeout_seconds,
                read=settings.huawei_face_timeout_seconds,
                write=settings.huawei_face_timeout_seconds,
                pool=settings.huawei_face_timeout_seconds,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
            self._owns_client = True

    @property
    def endpoint(self) -> str:
        return self._settings.huawei_frs_endpoint.rstrip("/")

    @property
    def project_id(self) -> str:
        return self._settings.huawei_project_id

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> httpx.Response:
        url = f"{self.endpoint}{path}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        body_bytes: bytes | None = None
        if json_body is not None:

            body_bytes = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        signed_headers = self._signer.sign(method, url, headers, body=body_bytes)
        try:
            resp = await self._client.request(
                method,
                url,
                headers=signed_headers,
                content=body_bytes,
            )
        except httpx.TimeoutException as exc:
            raise VisionTimeoutError(f"FRS 请求超时: {method} {path}", details=str(exc)) from exc
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
            raise VisionUnavailableError(
                f"FRS 服务不可用: {method} {path}", details=str(exc)
            ) from exc

        if resp.status_code >= 500:
            logger.debug("FRS 5xx body: %s", resp.text[:500])
            raise VisionUnavailableError(
                f"FRS 返回 {resp.status_code}",
                details={"status": resp.status_code},
            )
        if resp.status_code in (401, 403):
            raise VisionError(
                "FRS 鉴权失败：AK/SK 或 project_id 无效",
                details={"status": resp.status_code},
            )
        if resp.status_code >= 400:
            logger.debug("FRS 4xx body: %s", resp.text[:500])
            raise VisionError(
                f"FRS 返回异常状态码 {resp.status_code}",
                details={"status": resp.status_code},
            )
        return resp

    @staticmethod
    def _parse_json(resp: httpx.Response) -> dict[str, Any]:
        """安全解析 JSON 响应，非 JSON 或非对象时抛 VisionError。"""
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("FRS 非 JSON body: %s", resp.text[:500])
            raise VisionError(
                "FRS 返回非 JSON 响应",
                details={"status": resp.status_code},
            ) from exc
        if not isinstance(data, dict):
            raise VisionError(
                "FRS 返回非对象 JSON",
                details={"status": resp.status_code},
            )
        return data

    # ---- 人脸检测 ----

    async def detect_faces(self, image_url: str, max_face: int = 10) -> list[dict[str, Any]]:
        """对单张图片 URL 执行人脸检测，返回人脸列表。

        每个人脸包含 bounding_box 与 attributes（年龄/性别等）。
        """
        path = f"/v2/{self.project_id}/face-detect"
        resp = await self._request(
            "POST",
            path,
            json_body={"image_url": image_url, "max_face": max_face},
        )
        data = self._parse_json(resp)
        return [f for f in data.get("faces", []) if isinstance(f, dict)]

    # ---- 人脸库管理 ----

    async def create_face_set(self, face_set_name: str, capacity: int = 100000) -> dict[str, Any]:
        """创建人脸库。"""
        path = f"/v2/{self.project_id}/face-sets"
        resp = await self._request(
            "POST",
            path,
            json_body={"face_set_name": face_set_name, "face_set_capacity": capacity},
        )
        return self._parse_json(resp)

    async def delete_face_set(self, face_set_name: str) -> None:
        """删除人脸库（幂等：不存在时记录日志不报错，失败重试 3 次）。"""
        path = f"/v2/{self.project_id}/face-sets/{face_set_name}"
        for attempt in range(3):
            try:
                await self._request("DELETE", path)
                return
            except (VisionError, VisionUnavailableError, VisionTimeoutError, httpx.HTTPError) as exc:
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))
                else:
                    logger.warning("删除人脸库 %s 重试耗尽（忽略）: %s", face_set_name, exc)

    async def add_face_by_url(
        self,
        face_set_name: str,
        image_url: str,
        external_image_id: str,
    ) -> str:
        """通过 URL 向人脸库添加人脸，返回 face_id。"""
        path = f"/v2/{self.project_id}/face-sets/{face_set_name}/faces"
        resp = await self._request(
            "POST",
            path,
            json_body={
                "image_url": image_url,
                "external_image_id": external_image_id,
            },
        )
        data = self._parse_json(resp)
        face_id = data.get("face_id")
        if not face_id:
            raise VisionError("FRS 添加人脸未返回 face_id", details=data)
        return str(face_id)

    async def search_face_by_id(
        self,
        face_set_name: str,
        face_id: str,
        top_n: int = 10,
        threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        """在人脸库中搜索与指定 face_id 相似的人脸，返回匹配列表。

        每条匹配包含 face_id / similarity / external_image_id。
        """
        path = f"/v2/{self.project_id}/face-sets/{face_set_name}/search"
        resp = await self._request(
            "POST",
            path,
            json_body={"face_id": face_id, "top_n": top_n, "threshold": threshold},
        )
        data = self._parse_json(resp)
        return [f for f in data.get("faces", []) if isinstance(f, dict)]
