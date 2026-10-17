"""回调基地址解析：确定 Immich 容器可访问本服务的 URL。

Immich 运行在 Docker 容器中，容器内 127.0.0.1 指向容器自身而非宿主。
因此工作流 webhook 回调地址必须使用 host.docker.internal（Docker Desktop
提供的宿主别名）而非 127.0.0.1。

解析优先级：
1. 显式配置 plugin_callback_base_url（非空则直接使用）
2. 自动探测：Immich 在容器中运行 → http://host.docker.internal:{port}
3. 兜底：http://127.0.0.1:{port}（同机非容器场景）
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from ..core.config import Settings

logger = logging.getLogger(__name__)

_HOOK_PATH = "/api/v1/plugin/hook"


def _extract_port(base_url: str) -> int:
    """从 URL 提取端口，缺省时按 scheme 补 80/443。"""
    parsed = urlparse(base_url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


async def _immich_runs_in_container(immich_base_url: str) -> bool:
    """检测 Immich 是否运行在 Docker 容器中。

    判断依据：宿主能访问 Immich API，但容器内 127.0.0.1 访问不到本服务。
    简化判断：Immich base_url 指向 127.0.0.1/localhost 且能 ping 通 → 容器场景。
    """
    parsed = urlparse(immich_base_url)
    return parsed.hostname in ("127.0.0.1", "localhost", "0.0.0.0")


async def resolve_callback_base_url(
    settings: Settings,
    *,
    immich_reachable: bool = True,
) -> str:
    """解析工作流回调基地址。

    参数:
        settings: 应用配置。
        immich_reachable: Immich 是否可达（不可达时跳过容器探测）。

    返回:
        回调基地址，如 http://host.docker.internal:8000
    """
    if settings.plugin_callback_base_url:
        return settings.plugin_callback_base_url.rstrip("/")

    port = _extract_port(settings.immich_base_url)
    if immich_reachable and await _immich_runs_in_container(settings.immich_base_url):
        suggested = f"http://host.docker.internal:{port}"
        logger.info("回调基地址自动探测: Immich 在容器中运行，使用 %s", suggested)
        return suggested

    suggested = f"http://127.0.0.1:{port}"
    logger.info("回调基地址自动探测: 使用 %s", suggested)
    return suggested


def resolve_hook_url(callback_base_url: str) -> str:
    """拼接完整 hook URL。"""
    return f"{callback_base_url.rstrip('/')}{_HOOK_PATH}"


async def verify_container_reachability(hook_url: str) -> bool:
    """从宿主验证 hook_url 是否可达（不进容器，仅做连通性参考）。"""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3.0) as client:
            health_url = hook_url.rsplit("/api/v1/plugin/hook", 1)[0] + "/health"
            resp = await client.get(health_url)
            return resp.status_code == 200
    except Exception:
        return False
