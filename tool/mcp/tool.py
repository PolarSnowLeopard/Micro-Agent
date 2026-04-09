"""MCPTool：将远程 MCP 工具适配为统一的 Tool 接口。

Agent 不感知工具是本地还是远程——都通过 Tool.execute() 调用。
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from mcp import ClientSession
from mcp.types import TextContent

from tool.base import Tool, ToolResult


class MCPTool(Tool):
    """包装一个远程 MCP 服务器上的工具。"""

    def __init__(
        self,
        *,
        session: ClientSession,
        name: str,
        description: str,
        parameters: dict[str, Any],
        server_id: str,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._session = session
        self.server_id = server_id

    async def execute(self, **kwargs: Any) -> ToolResult:
        if not self._session:
            return ToolResult(error="MCP 会话未连接")
        try:
            result = await self._session.call_tool(self.name, kwargs)
            content = ", ".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            return ToolResult(output=content or "工具执行完成，无输出。")
        except Exception as e:
            logger.error(f"MCP 工具 '{self.name}' 执行失败: {e}")
            return ToolResult(error=f"MCP 工具执行失败: {e}")
