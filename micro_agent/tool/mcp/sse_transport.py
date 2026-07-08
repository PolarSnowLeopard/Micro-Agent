"""MCP SSE endpoint 解析。

服务库只存 `/sse` URL。握手时服务端返回 `/messages/?session_id=…`（session 每次不同），
客户端须把它拼到 SSE 前缀下。经 nginx 路径反代时，SDK 默认 urljoin 会拼到站点根。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urljoin

from mcp.client.sse import sse_client as _sdk_sse_client


def resolve_sse_endpoint(sse_url: str, endpoint: str) -> str:
    if endpoint.startswith("/"):
        prefix, _, _ = sse_url.rpartition("/sse")
        if prefix:
            return prefix + endpoint
    return urljoin(sse_url, endpoint)


@asynccontextmanager
async def sse_client(url: str, **kwargs: Any) -> AsyncIterator[tuple[Any, Any]]:
    import mcp.client.sse as mcp_sse

    orig = mcp_sse.urljoin
    mcp_sse.urljoin = resolve_sse_endpoint
    try:
        async with _sdk_sse_client(url, **kwargs) as streams:
            yield streams
    finally:
        mcp_sse.urljoin = orig
