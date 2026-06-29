"""真实 MCP 与显式假服务共享的工具会话。"""

from __future__ import annotations

import re
from typing import Any, Callable

from micro_agent.simulation.logging_mcp_tool import LoggingMCPTool
from micro_agent.simulation.sandbox_tool import SandboxTool
from micro_agent.simulation.trace_records import ToolCallRecord
from micro_agent.tool.mcp.connection import MCPConnectionManager, ServerConfig
from micro_agent.tool.registry import ToolRegistry
from micro_agent.tool.terminate import Terminate


class ServiceConnectionError(Exception):
    def __init__(self, reason: str, suggestion: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.suggestion = suggestion or "请检查远程 MCP 服务配置与可用性"


class ServiceToolSession:
    """连接服务、暴露工具，并统一收集调用事实。"""

    def __init__(
        self,
        services: list[dict[str, Any]],
        *,
        connection: MCPConnectionManager | None = None,
    ) -> None:
        self.services = services
        self.tools = ToolRegistry()
        self.tools.register(Terminate())
        self._connection = connection or MCPConnectionManager()
        self._mcp_tools: list[LoggingMCPTool] = []
        self._sandboxes: list[SandboxTool] = []
        self._statuses: list[dict[str, Any]] = []
        self._tool_names: set[str] = set()

    async def connect(self, check_cancel: Callable[[], None] | None = None) -> None:
        for service in self.services:
            if check_cancel:
                check_cancel()
            service_id = str(service.get("id") or "")
            service_name = str(service.get("name") or service_id or "?")
            if _is_fake(service):
                self._add_sandbox(service, service_id, service_name)
            else:
                await self._add_mcp(service, service_id, service_name)

    async def close(self) -> None:
        await self._connection.disconnect_all()

    def statuses(self) -> list[dict[str, Any]]:
        return list(self._statuses)

    def records(self) -> list[ToolCallRecord]:
        rows = [record for tool in self._sandboxes for record in tool.call_log]
        rows.extend(
            record
            for tool in self._mcp_tools
            for record in tool.call_log
        )
        return sorted(rows, key=lambda record: record.timestamp)

    def resolve_service(self, tool_name: str) -> dict[str, str] | None:
        for row in self.statuses():
            if tool_name in row["tools"]:
                return {"serviceId": row["id"], "serviceName": row["name"]}
        return None

    async def _add_mcp(self, service: dict[str, Any], service_id: str, service_name: str) -> None:
        method = str(service.get("mcpMethod") or service.get("method") or "sse").lower()
        endpoint = str(service.get("mcpUrl") or service.get("url") or "").strip()
        if method not in {"sse", "http", "streamable_http", "streamable-http"}:
            raise ServiceConnectionError(f"真实 MCP 配置无效 [{service_name}]：不支持 {method}")
        if not endpoint.startswith(("http://", "https://")):
            raise ServiceConnectionError(f"真实 MCP 配置无效 [{service_name}]：缺少 HTTP 地址")
        server = ServerConfig(
            connection_type="sse" if method == "sse" else "streamable_http",
            server_url=endpoint,
            server_id=_sanitize(f"{service_id or service_name}_mcp"),
        )
        try:
            _, discovered = await self._connection.connect(server)
        except Exception as exc:
            raise ServiceConnectionError(f"MCP 连接失败 [{service_name}] {endpoint}: {exc}") from exc
        if not discovered:
            raise ServiceConnectionError(f"MCP 未暴露工具 [{service_name}]")

        prefix = _sanitize(service_id or service_name)
        tools = [LoggingMCPTool(
            inner,
            registered_name=self._unique(prefix, inner.name),
            service_id=service_id,
            service_name=service_name,
            transport=server.connection_type,
        ) for inner in discovered]
        for tool in tools:
            self.tools.register(tool)
        self._mcp_tools.extend(tools)
        self._statuses.append({
            "id": service_id, "name": service_name, "channel": "mcp",
            "tools": [tool.name for tool in tools],
        })

    def _add_sandbox(self, service: dict[str, Any], service_id: str, service_name: str) -> None:
        tool = SandboxTool(
            name=self._unique(_sanitize(service_id or service_name), "execute"),
            description=str(service.get("description") or f"调用服务 [{service_name}]"),
            service_id=service_id,
            service_name=service_name,
        )
        self.tools.register(tool)
        self._sandboxes.append(tool)
        self._statuses.append({
            "id": service_id, "name": service_name,
            "channel": "sandbox", "tools": [tool.name],
        })

    def _unique(self, prefix: str, name: str) -> str:
        base = _sanitize(f"{prefix}_{name}")
        alias = base
        index = 1
        while alias in self._tool_names:
            alias = f"{base}_{index}"
            index += 1
        self._tool_names.add(alias)
        return alias

    async def __aenter__(self) -> ServiceToolSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


def _sanitize(value: Any, max_len: int = 128) -> str:
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "srv"))
    return re.sub(r"_+", "_", ident).strip("_")[:max_len] or "srv"


def _is_fake(service: dict[str, Any]) -> bool:
    return service.get("isFake") is True
