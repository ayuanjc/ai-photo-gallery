"""图片代理服务：解决前端无法直接访问 Immich 缩略图（需 API Key）的问题。

三种模式：
1. proxy（默认）：本服务代理请求，添加 x-api-key 转发到 Immich
2. shared_link：创建 Immich 共享链接，返回免 Key URL
3. signed：生成带签名的限时 URL（预留）
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..core.config import Settings
from .immich import ImmichClient

logger = logging.getLogger(__name__)

_VALID_SIZES = {"thumbnail", "preview", "full"}
_DEFAULT_TIMEOUT = 15.0

# asset_id 仅允许字母/数字/连字符/下划线（防御纵深：路由层已校验，此处兜底）
_SAFE_ASSET_ID = re.compile(r"^[A-Za-z0-9\-_]+$")


def _validate_asset_id(asset_id: str) -> None:
    if not _SAFE_ASSET_ID.match(asset_id or ""):
        raise ThumbnailFetchError("非法 asset_id", asset_id=asset_id or "")


class ImageProxyService:
    def __init__(self, immich: ImmichClient, settings: Settings) -> None:
        self._immich = immich
        self._settings = settings

    async def fetch_thumbnail(
        self, asset_id: str, size: str = "preview"
    ) -> tuple[bytes, str, str]:
        """代理获取缩略图，返回 (image_bytes, content_type, etag)。"""
        _validate_asset_id(asset_id)
        if size not in _VALID_SIZES:
            size = "preview"

        path = f"/api/assets/{asset_id}/thumbnail"
        params = {"size": size} if size != "full" else {}

        client = await self._immich._get_client()
        resp = await client.get(
            path,
            params=params,
            timeout=_DEFAULT_TIMEOUT,
        )

        if resp.status_code != 200:
            logger.warning(
                "缩略图代理失败 asset=%s size=%s status=%d",
                asset_id, size, resp.status_code,
            )
            raise ThumbnailFetchError(
                f"Immich 返回 {resp.status_code}", asset_id=asset_id
            )

        content_type = resp.headers.get("content-type", "image/jpeg")
        etag = resp.headers.get("etag", "")
        return resp.content, content_type, etag

    async def fetch_original(
        self, asset_id: str
    ) -> tuple[bytes, str, str]:
        """代理获取原图，返回 (image_bytes, content_type, etag)。"""
        _validate_asset_id(asset_id)
        path = f"/api/assets/{asset_id}/original"

        client = await self._immich._get_client()
        resp = await client.get(path, timeout=_DEFAULT_TIMEOUT)

        if resp.status_code != 200:
            logger.warning(
                "原图代理失败 asset=%s status=%d",
                asset_id, resp.status_code,
            )
            raise ThumbnailFetchError(
                f"Immich 返回 {resp.status_code}", asset_id=asset_id
            )

        content_type = resp.headers.get("content-type", "image/jpeg")
        etag = resp.headers.get("etag", "")
        return resp.content, content_type, etag

    async def create_shared_link(
        self, asset_id: str, expires_in_seconds: int = 3600
    ) -> str | None:
        """创建 Immich 共享链接，返回免 Key URL。"""
        try:
            payload: dict[str, Any] = {
                "assetIds": [asset_id],
                "expiresAt": None,
            }
            if expires_in_seconds > 0:
                from datetime import datetime, timedelta, timezone
                expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
                payload["expiresAt"] = expiry.isoformat()

            result = await self._immich.request_json(
                "POST", "/api/shared-links", json=payload
            )
            if isinstance(result, dict):
                key = result.get("key")
                if key:
                    return f"{self._immich.base_url}/share/{key}"
        except Exception as exc:
            logger.warning("共享链接创建失败 asset=%s: %s", asset_id, exc)
        return None


class ThumbnailFetchError(Exception):
    def __init__(self, message: str, asset_id: str = "") -> None:
        super().__init__(message)
        self.asset_id = asset_id
