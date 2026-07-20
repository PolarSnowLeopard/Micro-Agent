"""PoC: SSRF via user-controlled MCP server URL in ServiceToolSession."""
from __future__ import annotations

import asyncio
import pytest

from micro_agent.simulation.service_tool_session import (
    ServiceConnectionError,
    ServiceToolSession,
)


class RecordingConnection:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def connect(self, server, *_a, **_kw):
        self.attempts.append(server.server_url)
        raise RuntimeError("should not reach network for blocked host")

    async def disconnect_all(self) -> None:
        pass


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:6379/sse",
    "http://localhost/sse",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5:8080/sse",
    "http://192.168.1.1/sse",
    "http://[::1]/sse",
])
def test_ssrf_blocks_internal_mcp_urls(url):
    conn = RecordingConnection()
    session = ServiceToolSession(
        [{
            "id": "svc",
            "name": "svc",
            "isFake": False,
            "mcpMethod": "sse",
            "mcpUrl": url,
        }],
        connection=conn,
    )
    with pytest.raises(ServiceConnectionError):
        asyncio.run(session.connect())
    assert conn.attempts == [], f"SSRF: connection attempted to {conn.attempts}"


def test_ssrf_allows_public_host_but_still_fails_gracefully():
    conn = RecordingConnection()
    session = ServiceToolSession(
        [{
            "id": "svc",
            "name": "svc",
            "isFake": False,
            "mcpMethod": "sse",
            "mcpUrl": "https://mcp.example.test/sse",
        }],
        connection=conn,
    )
    with pytest.raises(ServiceConnectionError):
        asyncio.run(session.connect())
    assert conn.attempts == ["https://mcp.example.test/sse"]
