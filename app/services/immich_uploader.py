"""Immich 资产上传客户端。

封装 Immich 资产上传接口：
    POST /api/assets（multipart/form-data）

特性：
- 异步（httpx.AsyncClient 复用连接池），复用 x-api-key 认证。
- 通过稳定 deviceAssetId 实现幂等去重（同一文件重复上传不产生重复资产）。
- 错误分类：连接失败 -> UPSTREAM_UNAVAILABLE，读超时 -> UPSTREAM_TIMEOUT，
  其他 4xx/协议异常 -> UPSTREAM_ERROR。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
from datetime import datetime
from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError

logger = logging.getLogger(__name__)


class ImmichAssetUploader:
    """Immich 资产异步上传器。"""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    @property
    def base_url(self) -> str:
        """返回 Immich 基础 URL。"""
        return self._settings.immich_base_url

    async def _get_client(self) -> httpx.AsyncClient:
        """获取（或惰性创建）httpx 客户端。"""
        if self._client is not None:
            return self._client
        timeout = httpx.Timeout(
            connect=self._settings.immich_connect_timeout_seconds,
            read=self._settings.immich_timeout_seconds,
            write=self._settings.immich_timeout_seconds,
            pool=self._settings.immich_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"x-api-key": self._settings.immich_api_key, "Accept": "application/json"},
        )
        return self._client

    async def aclose(self) -> None:
        """关闭自建的 httpx 客户端（若持有所有权）。"""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _gen_device_asset_id(file_path: str, mtime_ns: int) -> str:
        """基于路径与修改时间生成稳定 deviceAssetId（幂等去重）。"""
        raw = f"{file_path}:{mtime_ns}".encode()
        return hashlib.sha256(raw).hexdigest()[:32]

    @staticmethod
    def _guess_mime(file_path: str) -> str:
        """推断文件 MIME 类型，未知时回退 application/octet-stream。"""
        mime, _ = mimetypes.guess_type(file_path)
        return mime or "application/octet-stream"

    @staticmethod
    def _read_file(file_path: str) -> tuple[os.stat_result, bytes]:
        """同步读取文件元信息与内容（供 asyncio.to_thread 包装，避免阻塞事件循环）。"""
        st = os.stat(file_path)
        with open(file_path, "rb") as fh:
            return st, fh.read()

    async def upload(
        self,
        file_path: str,
        *,
        device_id: str,
        taken_at: datetime,
        is_favorite: bool = False,
    ) -> dict[str, Any]:
        """上传单个文件到 Immich 资产库。

        参数:
            file_path: 本地文件路径。
            device_id: Immich 设备 ID。
            taken_at: 拍摄时间（作为 fileCreatedAt）。
            is_favorite: 是否标记为收藏。

        返回:
            资产信息 dict（至少包含 id 与 status）。

        异常:
            UpstreamUnavailableError: Immich 不可用。
            UpstreamTimeoutError: 请求超时。
            UpstreamError: 上传被拒绝或响应异常。
        """
        st, file_data = await asyncio.to_thread(self._read_file, file_path)
        file_name = os.path.basename(file_path)
        device_asset_id = self._gen_device_asset_id(file_path, st.st_mtime_ns)

        mime = self._guess_mime(file_path)
        files = {"fileData": (file_name, file_data, mime)}
        data = {
            "deviceAssetId": device_asset_id,
            "deviceId": device_id,
            "fileCreatedAt": taken_at.isoformat(),
            "fileModifiedAt": datetime.fromtimestamp(
                st.st_mtime, tz=taken_at.tzinfo
            ).isoformat(),
            "isFavorite": "true" if is_favorite else "false",
            "assetType": "image",
        }

        client = await self._get_client()
        try:
            resp = await client.post("/api/assets", files=files, data=data)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(
                f"Immich 上传超时: {file_name}", details=str(exc)
            ) from exc
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
            raise UpstreamUnavailableError(
                f"Immich 上传服务不可用: {file_name}", details=str(exc)
            ) from exc

        if resp.status_code in (401, 403):
            raise UpstreamError(
                "Immich API Key 无效或无上传权限", details={"status": resp.status_code}
            )
        if resp.status_code not in (200, 201):
            raise UpstreamError(
                "Immich 上传失败",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        return self._parse_asset(resp)

    @staticmethod
    def _parse_asset(resp: httpx.Response) -> dict[str, Any]:
        """解析上传响应，提取资产 id 与 status。"""
        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamError(
                "Immich 上传返回非 JSON 响应", details={"status": resp.status_code}
            ) from exc
        if not isinstance(data, dict):
            raise UpstreamError("Immich 上传响应结构异常", details=data)
        return {"id": data.get("id"), "status": data.get("status"), "raw": data}