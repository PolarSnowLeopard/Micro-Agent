from contextlib import AsyncExitStack
from typing import List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from app.logger import logger
from app.tool.base import BaseTool, ToolResult
from app.tool.tool_collection import ToolCollection


class MCPClientTool(BaseTool):
    """代表可以从客户端调用MCP服务器上的工具代理。"""

    session: Optional[ClientSession] = None

    async def execute(self, **kwargs) -> ToolResult:
        """通过向MCP服务器发起远程调用来执行工具。"""
        if not self.session:
            return ToolResult(error="未连接到MCP服务器")

        try:
            result = await self.session.call_tool(self.name, kwargs)
            content_str = ", ".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            return ToolResult(output=content_str or "未返回输出。")
        except Exception as e:
            return ToolResult(error=f"执行工具时出错: {str(e)}")


class MCPClients(ToolCollection):
    """
    一个工具集合，用于连接到MCP服务器并通过模型上下文协议管理可用工具。
    """

    session: Optional[ClientSession] = None
    exit_stack: AsyncExitStack = None
    description: str = "用于服务器交互的MCP客户端工具"

    def __init__(self):
        super().__init__()  # 使用空工具列表初始化
        self.name = "mcp"  # 保持名称以向后兼容
        self.exit_stack = AsyncExitStack()

    async def connect_sse(self, server_url: str) -> None:
        """使用SSE传输连接到MCP服务器。"""
        if not server_url:
            raise ValueError("需要服务器URL。")
        if self.session:
            await self.disconnect()

        streams_context = sse_client(url=server_url)
        streams = await self.exit_stack.enter_async_context(streams_context)
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(*streams)
        )

        await self._initialize_and_list_tools()

    async def connect_stdio(self, command: str, args: List[str]) -> None:
        """使用stdio传输连接到MCP服务器。"""
        if not command:
            raise ValueError("需要服务器命令。")
        if self.session:
            await self.disconnect()

        server_params = StdioServerParameters(command=command, args=args)
        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        read, write = stdio_transport
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(read, write)
        )

        await self._initialize_and_list_tools()

    async def _initialize_and_list_tools(self) -> None:
        """初始化会话并填充工具映射。"""
        if not self.session:
            raise RuntimeError("会话未初始化。")

        await self.session.initialize()
        response = await self.session.list_tools()

        # 清除现有工具
        self.tools = tuple()
        self.tool_map = {}

        # 为每个服务器工具创建适当的工具对象
        for tool in response.tools:
            server_tool = MCPClientTool(
                name=tool.name,
                description=tool.description,
                parameters=tool.inputSchema,
                session=self.session,
            )
            self.tool_map[tool.name] = server_tool

        self.tools = tuple(self.tool_map.values())
        logger.info(
            f"已连接到服务器，具有以下工具: {[tool.name for tool in response.tools]}"
        )

    async def disconnect(self) -> None:
        """断开与MCP服务器的连接并清理资源。"""
        if self.session and self.exit_stack:
            await self.exit_stack.aclose()
            self.session = None
            self.tools = tuple()
            self.tool_map = {}
            logger.info("已从MCP服务器断开连接")
