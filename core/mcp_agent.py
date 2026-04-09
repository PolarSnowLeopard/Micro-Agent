"""MCPAgent：在 Agent 基础上增加 MCP 连接管理。

关键约束：不重写 run() 循环。只增加 connect/disconnect 和 async with 支持。
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from loguru import logger

from core.agent import Agent
from core.llm import LLM
from core.schema import AgentEvent
from tool.mcp.connection import MCPConnectionManager, ServerConfig
from tool.mcp.tool import MCPTool
from tool.registry import ToolRegistry
from tool.terminate import Terminate


class MCPAgent(Agent):
    """支持 MCP 服务器连接的 Agent。

    用法：
        async with MCPAgent(llm=llm) as agent:
            await agent.connect(ServerConfig(server_url="http://..."))
            async for event in agent.run("你好"):
                print(event)
    """

    def __init__(
        self,
        *,
        name: str = "mcp_agent",
        llm: LLM,
        tools: Optional[ToolRegistry] = None,
        system_prompt: str = "",
        next_step_prompt: str = "",
        max_steps: int = 40,
        **kwargs: object,
    ):
        tools = tools or ToolRegistry()
        if "terminate" not in tools:
            tools.register(Terminate())

        super().__init__(
            name=name,
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            next_step_prompt=next_step_prompt,
            max_steps=max_steps,
            **kwargs,
        )
        self._conn = MCPConnectionManager()
        self._connected: list[str] = []

    async def connect(self, config: ServerConfig) -> str:
        """连接到一个 MCP 服务器，自动将其工具注册到 ToolRegistry。"""
        server_id, tools = await self._conn.connect(config)
        for tool in tools:
            self.tools.register(tool, namespace=server_id)
        self._connected.append(server_id)
        logger.info(
            f"[{self.name}] 已连接 {server_id}，"
            f"当前工具: {self.tools.list_names()}"
        )
        return server_id

    async def connect_many(self, configs: list[ServerConfig]) -> list[str]:
        """批量连接多个 MCP 服务器。"""
        ids = []
        for cfg in configs:
            try:
                sid = await self.connect(cfg)
                ids.append(sid)
            except Exception as e:
                logger.error(f"连接 MCP 服务器失败: {e}")
        return ids

    async def disconnect(self, server_id: Optional[str] = None) -> None:
        """断开连接。不指定 server_id 则断开全部。"""
        if server_id:
            self.tools.unregister_by_namespace(server_id)
            await self._conn.disconnect(server_id)
            if server_id in self._connected:
                self._connected.remove(server_id)
        else:
            for sid in list(self._connected):
                self.tools.unregister_by_namespace(sid)
            self._connected.clear()
            await self._conn.disconnect_all()

    async def __aenter__(self) -> MCPAgent:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()
