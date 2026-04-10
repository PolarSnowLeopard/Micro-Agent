"""MCP 连接管理器。

用 async with 管理生命周期，彻底替代旧版 fire-and-forget cleanup 模式。
与 ToolRegistry 完全解耦——只负责连接，不负责工具注册。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from micro_agent.tool.mcp.tool import MCPTool


@dataclass
class ServerConfig:
    """MCP 服务器连接配置。"""

    connection_type: str = "sse"  # "sse" | "stdio"
    server_url: Optional[str] = None  # SSE 模式
    command: Optional[str] = None  # stdio 模式
    args: Optional[list[str]] = None  # stdio 模式
    server_id: Optional[str] = None


class MCPConnectionManager:
    """管理多个 MCP 服务器的连接生命周期。

    用法：
        async with MCPConnectionManager() as conn:
            server_id, tools = await conn.connect(ServerConfig(server_url="http://..."))
            # tools 是 list[MCPTool]，可注册到 ToolRegistry
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._counter = 0

    async def connect(self, config: ServerConfig) -> tuple[str, list[MCPTool]]:
        """连接到 MCP 服务器，返回 (server_id, 工具列表)。"""
        server_id = config.server_id or self._gen_id(config.connection_type)
        stack = AsyncExitStack()

        try:
            if config.connection_type == "sse":
                if not config.server_url:
                    raise ValueError("SSE 连接需要 server_url")
                session = await self._connect_sse(stack, config.server_url)
            elif config.connection_type == "stdio":
                if not config.command:
                    raise ValueError("stdio 连接需要 command")
                session = await self._connect_stdio(
                    stack, config.command, config.args or []
                )
            else:
                raise ValueError(f"不支持的连接类型: {config.connection_type}")

            await session.initialize()
            self._sessions[server_id] = session
            self._stacks[server_id] = stack

            response = await session.list_tools()
            tools = [
                MCPTool(
                    session=session,
                    name=t.name,
                    description=t.description or "",
                    parameters=t.inputSchema,
                    server_id=server_id,
                )
                for t in response.tools
            ]

            logger.info(
                f"已连接 MCP 服务器 {server_id}，"
                f"工具: {[t.name for t in tools]}"
            )
            return server_id, tools

        except Exception:
            await stack.aclose()
            raise

    async def disconnect(self, server_id: str) -> None:
        """断开指定服务器连接。"""
        stack = self._stacks.pop(server_id, None)
        self._sessions.pop(server_id, None)
        if stack:
            try:
                await stack.aclose()
                logger.info(f"已断开 MCP 服务器 {server_id}")
            except Exception as e:
                logger.warning(f"断开 {server_id} 时异常（已忽略）: {e}")

    async def disconnect_all(self) -> None:
        """断开所有服务器连接。"""
        for sid in list(self._sessions):
            await self.disconnect(sid)

    def get_session(self, server_id: str) -> Optional[ClientSession]:
        return self._sessions.get(server_id)

    def server_ids(self) -> list[str]:
        return list(self._sessions.keys())

    @staticmethod
    async def _connect_sse(
        stack: AsyncExitStack, url: str
    ) -> ClientSession:
        streams = await stack.enter_async_context(sse_client(url=url))
        return await stack.enter_async_context(ClientSession(*streams))

    @staticmethod
    async def _connect_stdio(
        stack: AsyncExitStack, command: str, args: list[str]
    ) -> ClientSession:
        transport = await stack.enter_async_context(
            stdio_client(StdioServerParameters(command=command, args=args))
        )
        return await stack.enter_async_context(ClientSession(*transport))

    def _gen_id(self, conn_type: str) -> str:
        self._counter += 1
        return f"{conn_type}_{self._counter}"

    async def __aenter__(self) -> MCPConnectionManager:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect_all()
