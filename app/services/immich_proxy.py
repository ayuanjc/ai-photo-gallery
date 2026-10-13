"""Immich 反向代理：将本项目 8000 端口作为 Immich 前端入口。

- HTTP catch-all：未命中本项目路由的请求全部转发到 Immich（透传方法/头/体/查询）。
- HTML 注入：GET 的 text/html 响应在 </head> 前注入 /static/inject.js，
  在 Immich 侧边栏添加“AI 图库助手”管理入口。
- WebSocket 代理：透传 Immich 的 socket.io 实时连接；上游不可用时关闭，
  客户端自动降级 polling。

注意事项：
- hop-by-hop 头（connection/keep-alive/transfer-encoding 等）不透传。
- 流式响应（缩略图/视频/上传）用流转发，避免大文件全量缓冲。
- 缓冲策略仅用于需要 HTML 注入的 GET text/html 响应（Immich SSR 页面体量小）。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

from ..core.config import Settings, get_settings

logger = logging.getLogger(__name__)

INJECT_SNIPPET = '<script src="/static/inject.js" defer></script>'

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}

_MAX_HTML_BUFFER = 2 * 1024 * 1024


def _filter_headers(headers: Any, extra: set[str] | None = None) -> Headers:
    drop = _HOP_BY_HOP | (extra or set())
    result = Headers()
    result._list = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in headers.items()
        if k.lower() not in drop
    ]
    return result


def _inject_html(body: bytes) -> bytes:
    text = body.decode("utf-8", errors="replace")
    if "__A01_PROXY_INJECTED__" in text:
        return body
    marker = "<!-- __A01_PROXY_INJECTED__ -->"
    snippet = INJECT_SNIPPET + marker
    lowered = text.lower()
    if "</head>" in lowered:
        idx = lowered.index("</head>")
        return (text[:idx] + snippet + text[idx:]).encode("utf-8")
    if "</body>" in lowered:
        idx = lowered.rindex("</body>")
        return (text[:idx] + snippet + text[idx:]).encode("utf-8")
    return body


async def proxy_request(request: Request, path: str) -> Response:
    """转发未命中的 HTTP 请求到 Immich。"""
    settings: Settings = get_settings()
    base_url = settings.immich_base_url.rstrip("/")

    target = f"{base_url}/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers = _filter_headers(request.headers)
    body = request.stream() if request.method in ("POST", "PUT", "PATCH", "DELETE") else None

    client: httpx.AsyncClient = getattr(request.app.state, "proxy_client", None)
    owns = False
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        owns = True

    try:
        resp = await client.request(
            request.method,
            target,
            headers=headers,
            content=body,
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        if owns:
            await client.aclose()
        return Response(
            content='{"error":{"code":"UPSTREAM_TIMEOUT","message":"Immich 响应超时"}}',
            status_code=504,
            media_type="application/json",
        )
    except httpx.ConnectError:
        if owns:
            await client.aclose()
        return Response(
            content='{"error":{"code":"UPSTREAM_UNAVAILABLE","message":"Immich 服务不可用"}}',
            status_code=502,
            media_type="application/json",
        )

    content_type = resp.headers.get("content-type", "")
    is_html = "text/html" in content_type.lower()
    is_stream = resp.headers.get("transfer-encoding", "").lower() == "chunked"

    if is_html and request.method in ("GET", "HEAD") and not is_stream:
        raw = resp.content
        if len(raw) <= _MAX_HTML_BUFFER:
            injected = _inject_html(raw)
            resp_headers = _filter_headers(resp.headers)
            if owns:
                await client.aclose()
            return Response(
                content=injected,
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=content_type or None,
            )

    if owns:
        await client.aclose()
    resp_headers = _filter_headers(resp.headers)

    if request.method == "HEAD":
        return Response(status_code=resp.status_code, headers=resp_headers, media_type=content_type or None)

    async def stream_iter():
        async for chunk in resp.aiter_bytes():
            yield chunk
        await resp.aclose()

    return StreamingResponse(
        stream_iter(),
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


async def proxy_websocket(websocket: WebSocket, path: str) -> None:
    """透传 Immich 的 WebSocket（socket.io）连接；上游不可用时关闭，客户端降级 polling。"""
    import asyncio
    import contextlib

    import websockets

    settings: Settings = get_settings()
    base_url = settings.immich_base_url.rstrip("/")

    query = f"?{websocket.url.query}" if websocket.url.query else ""
    ws_host = base_url.replace("http://", "").replace("https://", "")
    upstream_url = f"ws://{ws_host}/{path}{query}"

    await websocket.accept()
    cookies = websocket.headers.get("cookie", "")
    api_key = websocket.headers.get("x-api-key", "")
    subprotocols = websocket.scope.get("subprotocols") or []

    headers: dict[str, str] = {}
    if cookies:
        headers["Cookie"] = cookies
    if api_key:
        headers["x-api-key"] = api_key

    try:
        async with websockets.connect(
            upstream_url,
            additional_headers=headers,
            subprotocols=subprotocols or None,
            open_timeout=5,
        ) as upstream:
            async def client_to_upstream() -> None:
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await upstream.send(data)
                except (WebSocketDisconnect, RuntimeError):
                    return

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    return

            t1 = asyncio.create_task(client_to_upstream())
            t2 = asyncio.create_task(upstream_to_client())
            done, pending = await asyncio.wait(
                {t1, t2}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    except Exception:
        logger.exception("WebSocket 代理异常: %s", path)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)