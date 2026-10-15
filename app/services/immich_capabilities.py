"""Immich 能力探测模块。

启动时探测 Immich 上游版本与功能集，结果挂到 app.state.capabilities，
供搜索编排、写回层、插件桥等模块按能力降级。

探测项（7 项）：
1. 版本号            — GET /api/server/about
2. 标签写回          — GET /api/tags
3. 相册写回          — GET /api/albums
4. 人物写回          — GET /api/people
5. 新版搜索 filters  — POST /api/search/metadata with filter.updatedAt
6. facets            — 同上，检查响应含 facets 字段
7. assetIds 搜索     — POST /api/search/smart with assetIds（探测是否被 zod 剥离）
8. 插件              — GET /api/plugins
9. 工作流            — GET /api/workflows

每项独立 try/except，失败标记 False/None，不阻塞启动。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from .immich import ImmichClient

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 8.0


class ImmichCapabilities(BaseModel):
    version: str | None = None
    supports_writeback_tags: bool = False
    supports_writeback_albums: bool = False
    supports_writeback_people: bool = False
    supports_new_search_filters: bool = False
    supports_facets: bool = False
    supports_asset_ids_in_search: bool = False
    supports_plugins: bool = False
    supports_workflows: bool = False
    plugin_methods: list[str] = []
    workflow_triggers: list[str] = []
    probed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    async def probe(cls, immich: ImmichClient) -> ImmichCapabilities:
        cap = cls(probed_at=datetime.now(timezone.utc).isoformat())

        await _probe_version(immich, cap)
        await _probe_tags(immich, cap)
        await _probe_albums(immich, cap)
        await _probe_people(immich, cap)
        await _probe_search_filters_and_facets(immich, cap)
        await _probe_asset_ids(immich, cap)
        await _probe_plugins(immich, cap)
        await _probe_workflows(immich, cap)

        logger.info(
            "Immich 能力探测完成: version=%s tags=%s albums=%s people=%s "
            "filters=%s facets=%s assetIds=%s plugins=%s workflows=%s",
            cap.version,
            cap.supports_writeback_tags,
            cap.supports_writeback_albums,
            cap.supports_writeback_people,
            cap.supports_new_search_filters,
            cap.supports_facets,
            cap.supports_asset_ids_in_search,
            cap.supports_plugins,
            cap.supports_workflows,
        )
        return cap


async def _probe_version(immich: ImmichClient, cap: ImmichCapabilities) -> None:
    try:
        data = await immich.request_json("GET", "/api/server/about")
        if isinstance(data, dict):
            info = data.get("info", data)
            version = info.get("version") if isinstance(info, dict) else None
            cap.version = version
    except Exception as exc:
        logger.debug("版本探测失败: %s", exc)


async def _probe_tags(immich: ImmichClient, cap: ImmichCapabilities) -> None:
    try:
        data = await immich.request_json("GET", "/api/tags")
        cap.supports_writeback_tags = isinstance(data, list)
    except Exception as exc:
        logger.debug("标签写回探测失败: %s", exc)


async def _probe_albums(immich: ImmichClient, cap: ImmichCapabilities) -> None:
    try:
        data = await immich.request_json("GET", "/api/albums")
        cap.supports_writeback_albums = isinstance(data, list)
    except Exception as exc:
        logger.debug("相册写回探测失败: %s", exc)


async def _probe_people(immich: ImmichClient, cap: ImmichCapabilities) -> None:
    try:
        data = await immich.request_json("GET", "/api/people")
        cap.supports_writeback_people = isinstance(data, dict) and "people" in data
    except Exception as exc:
        logger.debug("人物写回探测失败: %s", exc)


async def _probe_search_filters_and_facets(
    immich: ImmichClient, cap: ImmichCapabilities
) -> None:
    try:
        payload = {
            "searchTerm": "",
            "filter": {"updatedAt": {"start": "2020-01-01T00:00:00.000Z"}},
        }
        data = await immich.request_json(
            "POST", "/api/search/metadata", json=payload
        )
        if isinstance(data, dict):
            cap.supports_new_search_filters = "assets" in data
            cap.supports_facets = "facets" in data
    except Exception as exc:
        logger.debug("搜索 filters/facets 探测失败: %s", exc)


async def _probe_asset_ids(immich: ImmichClient, cap: ImmichCapabilities) -> None:
    sentinel = "00000000-0000-0000-0000-000000000000"
    try:
        payload = {"query": "test", "size": 1, "assetIds": [sentinel]}
        data = await immich.request_json(
            "POST", "/api/search/smart", json=payload
        )
        items = ImmichClient.extract_items(data)
        ids = {item.get("id") for item in items}
        cap.supports_asset_ids_in_search = sentinel in ids
    except Exception as exc:
        logger.debug("assetIds 探测失败: %s", exc)


async def _probe_plugins(immich: ImmichClient, cap: ImmichCapabilities) -> None:
    try:
        data = await immich.request_json("GET", "/api/plugins")
        if isinstance(data, list) and data:
            cap.supports_plugins = True
            methods: list[str] = []
            for plugin in data:
                if isinstance(plugin, dict):
                    methods.extend(
                        m.get("name", "")
                        for m in plugin.get("methods", [])
                        if isinstance(m, dict) and m.get("name")
                    )
            cap.plugin_methods = sorted(set(methods))
        elif isinstance(data, dict) and data.get("plugins"):
            cap.supports_plugins = True
            cap.plugin_methods = []
    except Exception as exc:
        logger.debug("插件探测失败: %s", exc)


async def _probe_workflows(immich: ImmichClient, cap: ImmichCapabilities) -> None:
    try:
        data = await immich.request_json("GET", "/api/workflows")
        if isinstance(data, list) and data:
            cap.supports_workflows = True
            cap.workflow_triggers = sorted({
                w.get("trigger", "")
                for w in data
                if isinstance(w, dict) and w.get("trigger")
            })
        elif isinstance(data, dict) and data.get("workflows"):
            cap.supports_workflows = True
    except Exception as exc:
        logger.debug("工作流探测失败: %s", exc)