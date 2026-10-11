"""Immich 上游服务客户端。

封装 Immich 开源照片管理平台的语义搜索接口：
    POST /api/search/smart

特性：
- 异步（httpx.AsyncClient 复用连接池）。
- 超时（连接/总体分设）与指数退避重试。
- 错误分类：连接失败 -> UPSTREAM_UNAVAILABLE，读超时 -> UPSTREAM_TIMEOUT，
  其他 4xx/协议异常 -> UPSTREAM_ERROR。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError

logger = logging.getLogger(__name__)


class ImmichClient:
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
        return self._settings.immich_base_url

    async def _get_client(self) -> httpx.AsyncClient:
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
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        client = await self._get_client()
        max_retries = max(0, self._settings.immich_max_retries)
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            resp: httpx.Response | None = None
            try:
                resp = await client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeoutError(
                    f"Immich 请求超时: {method} {path}", details=str(exc)
                ) from exc
            except (httpx.ConnectError, httpx.HTTPError) as exc:
                last_exc = exc

            if resp is not None:
                if resp.status_code < 500:
                    return resp
                # 5xx 视为可重试
                last_exc = RespgenError(resp)

            if attempt < max_retries:
                await asyncio.sleep(self._settings.immich_retry_backoff_seconds * (2 ** attempt))

        raise UpstreamUnavailableError(
            f"Immich 服务不可用: {method} {path}", details=str(last_exc)
        )

    async def smart_search(self, query: str, k: int = 50) -> list[dict]:
        """调用 POST /api/search/smart 执行语义搜索，返回资产列表。

        兼容 Immich 多种响应格式（assets.items / assets(list) / items / 直接 list），
        并对未携带 score 的资产按排名补默认相似度（越靠前越高）。
        """
        resp = await self._request_with_retry(
            "POST",
            "/api/search/smart",
            json={"query": query, "k": k, "withExif": True},
        )
        if resp.status_code == 400:
            raise UpstreamError(
                "Immich 语义搜索请求被拒绝",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        if resp.status_code in (401, 403):
            raise UpstreamError(
                "Immich API Key 无效或无权限", details={"status": resp.status_code}
            )
        if resp.status_code != 200:
            raise UpstreamError(
                "Immich 返回异常状态码",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )

        data = resp.json()
        items = self._extract_items(data)
        return self._fill_missing_score(items)

    async def get_asset(self, asset_id: str) -> dict:
        """调用 GET /api/assets/{id} 获取单条资产详情（含 people 人物分组信息）。"""
        resp = await self._request_with_retry("GET", f"/api/assets/{asset_id}")
        if resp.status_code != 200:
            raise UpstreamError(
                "Immich 获取资产失败",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        return resp.json()

    async def similar_assets(self, asset_id: str, k: int = 50) -> list[dict]:
        """调用 POST /api/search/smart 以图搜图（query 传 asset id），返回带 score 的相似资产。"""
        resp = await self._request_with_retry(
            "POST",
            "/api/search/smart",
            json={"query": asset_id, "k": k, "withExif": False},
        )
        if resp.status_code != 200:
            raise UpstreamError(
                "Immich 相似搜索失败",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        data = resp.json()
        return self._fill_missing_score(self._extract_items(data))

    async def list_people(self) -> list[dict]:
        """调用 GET /api/people 获取人物列表（用于健康检查与人物可用性判断）。"""
        resp = await self._request_with_retry("GET", "/api/people")
        if resp.status_code != 200:
            raise UpstreamError(
                "Immich 获取人物列表失败",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        data = resp.json()
        if isinstance(data, dict):
            people = data.get("people", [])
            if isinstance(people, list):
                return [p for p in people if isinstance(p, dict)]
        return []

    @staticmethod
    def _extract_items(data: Any) -> list[dict]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            assets = data.get("assets")
            if isinstance(assets, dict):
                items = assets.get("items", [])
            elif isinstance(assets, list):
                items = assets
            elif "items" in data:
                items = data["items"]
            else:
                items = []
            return [item for item in items if isinstance(item, dict)]
        return []

    @staticmethod
    def _fill_missing_score(items: list[dict]) -> list[dict]:
        total = len(items)
        for idx, item in enumerate(items):
            if item.get("score") is None:
                # Immich 部分版本不返回显式分数，按排名线性衰减补分。
                item["score"] = 1.0 - (idx / total) if total > 1 else 1.0
        return items

    # ---- 资源 URL 构建 ----
    def thumbnail_url(self, asset_id: str, size: str = "preview") -> str:
        return f"{self.base_url}/api/assets/{asset_id}/thumbnail?size={size}"

    def original_url(self, asset_id: str) -> str:
        return f"{self.base_url}/api/assets/{asset_id}/original"


class RespgenError(Exception):
    """包装非 2xx 响应用于重试逻辑。"""

    def __init__(self, resp: httpx.Response) -> None:
        super().__init__(f"HTTP {resp.status_code}")
        self.resp = resp