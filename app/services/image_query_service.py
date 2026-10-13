"""以图搜图服务：从参考图 URL 提取 asset_id，调 Immich similar_assets 获取相似图。

设计要点：
- 从 Immich 风格 URL（/api/assets/{id}/...）中提取 asset_id。
- 对每个 asset_id 调 Immich similar_assets 获取相似图列表。
- 任一图失败时跳过该图，记录 warnings，不抛异常。
- 返回去重后的 asset_id 集合。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from ..core.config import Settings
from ..core.errors import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError
from ..utils.url_safety import validate_url_or_raise
from .immich import ImmichClient

logger = logging.getLogger(__name__)

_ASSET_ID_PATTERN = re.compile(r"/api/assets/([^/?]+)")


class ImageQueryService:
    """以图搜图服务：将参考图 URL 解析为相似 asset_id 集合。"""

    def __init__(self, settings: Settings, immich: ImmichClient) -> None:
        self._settings = settings
        self._immich = immich

    async def resolve_asset_ids_from_images(
        self,
        image_urls: list[str],
        top_k: int = 50,
    ) -> tuple[set[str], list[str]]:
        """对每张参考图调 Immich similar_assets，返回去重后的 asset_id 集合。

        参数:
            image_urls: 参考图 URL 列表（Immich 风格 URL）。
            top_k: 每张图取相似图的数量上限。

        返回:
            (asset_id 集合, warnings 列表)
        """
        result: set[str] = set()
        warnings: list[str] = []

        for url in image_urls:
            asset_id = self._extract_asset_id(url)
            if asset_id is None:
                warnings.append(f"无法从 URL 提取 asset_id，已跳过: {url[:100]}")
                continue

            try:
                similar = await self._immich.similar_assets(asset_id, k=top_k)
            except (UpstreamUnavailableError, UpstreamTimeoutError, UpstreamError) as exc:
                logger.warning("相似图搜索失败, asset_id=%s: %s", asset_id, exc)
                warnings.append(f"相似图搜索失败，已跳过: {asset_id}")
                continue
            except Exception as exc:
                logger.warning("相似图搜索异常, asset_id=%s: %s", asset_id, exc)
                warnings.append(f"相似图搜索异常，已跳过: {asset_id}")
                continue

            for item in similar:
                item_id = str(item.get("id", ""))
                if item_id:
                    result.add(item_id)

        return result, warnings

    @staticmethod
    def _extract_asset_id(url: str) -> str | None:
        """从 Immich 风格 URL 中提取 asset_id。

        支持 URL 格式:
            http://immich/api/assets/{id}/thumbnail
            http://immich/api/assets/{id}/original
            /api/assets/{id}

        同时对 URL 做 SSRF 安全校验，不安全时返回 None。
        """
        try:
            validate_url_or_raise(url)
        except ValueError:
            return None

        parsed = urlparse(url)
        match = _ASSET_ID_PATTERN.search(parsed.path)
        if match is None:
            return None
        asset_id = match.group(1)
        if not asset_id or not asset_id.strip():
            return None
        return asset_id.strip()