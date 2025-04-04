from contextlib import AsyncExitStack
from typing import Dict, List, Optional, Tuple
import asyncio

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
    server_id: str = None  # 添加服务器ID以跟踪工具所属的服务器
    original_name: str = None  # 存储原始工具名（无前缀）

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        session: ClientSession = None,
        server_id: str = None,
        original_name: str = None,
    ):
        """初始化MCP客户端工具。"""
        super().__init__(name=name, description=description, parameters=parameters)
        self.session = session
        self.server_id = server_id
        self.original_name = original_name

    async def execute(self, **kwargs) -> ToolResult:
        """通过向MCP服务器发起远程调用来执行工具。"""
        if not self.session:
            return ToolResult(error="未连接到MCP服务器")

        try:
            # 使用原始工具名称（无前缀）调用远程工具
            tool_name = self.original_name or self.name
            
            # 对于非终止工具，记录详细日志
            if tool_name.lower() != "terminate" and not self.name.endswith("_terminate"):
                logger.info(f"执行工具 {self.name}，实际调用远程工具: {tool_name}，参数: {kwargs}")
            else:
                # 对于终止工具，只记录简单的日志，避免与_handle_special_tool重复
                logger.info(f"准备执行终止工具 {self.name} -> {tool_name}")
            
            result = await self.session.call_tool(tool_name, kwargs)
            content_str = ", ".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            
            return ToolResult(output=content_str or "未返回输出。")
        except Exception as e:
            logger.error(f"执行工具 {self.name} -> {tool_name} 时出错: {str(e)}")
            return ToolResult(error=f"执行工具时出错: {str(e)}")


class MCPClients(ToolCollection):
    """
    一个工具集合，用于连接到多个MCP服务器并通过模型上下文协议管理可用工具。
    """

    sessions: Dict[str, ClientSession] = {}  # 服务器ID到会话的映射
    exit_stacks: Dict[str, AsyncExitStack] = {}  # 服务器ID到退出栈的映射
    description: str = "用于服务器交互的MCP客户端工具"
    
    # 保留默认会话以保持向后兼容
    session: Optional[ClientSession] = None
    exit_stack: Optional[AsyncExitStack] = None

    def __init__(self):
        super().__init__()  # 使用空工具列表初始化
        self.name = "mcp"  # 保持名称以向后兼容
        self.sessions = {}
        self.exit_stacks = {}

    async def connect_sse(self, server_url: str, server_id: str = None) -> str:
        """
        使用SSE传输连接到MCP服务器。
        
        参数:
            server_url: SSE服务器的URL
            server_id: 服务器的唯一标识符，如果未提供则自动生成
            
        返回:
            str: 服务器ID
        """
        if not server_url:
            raise ValueError("需要服务器URL。")
            
        # 如果未提供ID则生成唯一ID
        if not server_id:
            server_id = f"sse_{len(self.sessions)}"
            
        # 创建新的退出栈
        exit_stack = AsyncExitStack()
        
        try:
            # 将退出栈添加到字典中，这样可以在出错时找到它
            self.exit_stacks[server_id] = exit_stack

            # 连接到服务器
            streams_context = sse_client(url=server_url)
            streams = await exit_stack.enter_async_context(streams_context)
            session = await exit_stack.enter_async_context(
                ClientSession(*streams)
            )
            
            # 存储会话
            self.sessions[server_id] = session
            
            # 设置默认会话（向后兼容）
            if not self.session:
                self.session = session
                self.exit_stack = exit_stack

            # 初始化工具
            await self._initialize_and_list_tools(server_id)
            
            return server_id
        except Exception as e:
            # 如果在过程中出错，确保退出栈被正确关闭
            logger.error(f"连接SSE服务器时出错: {str(e)}")
            try:
                await exit_stack.aclose()
                # 移除对退出栈的引用
                if server_id in self.exit_stacks:
                    del self.exit_stacks[server_id]
                if server_id in self.sessions:
                    del self.sessions[server_id]
            except Exception as close_err:
                logger.error(f"关闭退出栈时出错: {str(close_err)}")
            raise

    async def connect_stdio(self, command: str, args: List[str], server_id: str = None) -> str:
        """
        使用stdio传输连接到MCP服务器。
        
        参数:
            command: 服务器命令
            args: 命令参数
            server_id: 服务器的唯一标识符，如果未提供则自动生成
            
        返回:
            str: 服务器ID
        """
        if not command:
            raise ValueError("需要服务器命令。")
            
        # 如果未提供ID则生成唯一ID
        if not server_id:
            server_id = f"stdio_{len(self.sessions)}"
        
        # 创建新的退出栈    
        exit_stack = AsyncExitStack()
        
        try:
            # 保存退出栈引用
            self.exit_stacks[server_id] = exit_stack

            # 连接到服务器
            server_params = StdioServerParameters(command=command, args=args)
            stdio_transport = await exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            read, write = stdio_transport
            session = await exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            
            # 存储会话
            self.sessions[server_id] = session
            
            # 设置默认会话（向后兼容）
            if not self.session:
                self.session = session
                self.exit_stack = exit_stack

            # 初始化工具
            await self._initialize_and_list_tools(server_id)
            
            return server_id
        except Exception as e:
            # 如果在过程中出错，确保退出栈被正确关闭
            logger.error(f"连接stdio服务器时出错: {str(e)}")
            try:
                await exit_stack.aclose()
                # 移除对退出栈的引用
                if server_id in self.exit_stacks:
                    del self.exit_stacks[server_id]
                if server_id in self.sessions:
                    del self.sessions[server_id]
            except Exception as close_err:
                logger.error(f"关闭退出栈时出错: {str(close_err)}")
            raise

    async def _initialize_and_list_tools(self, server_id: str) -> None:
        """
        初始化会话并填充工具映射。
        
        参数:
            server_id: 服务器的唯一标识符
        """
        session = self.sessions.get(server_id)
        if not session:
            raise RuntimeError(f"会话 {server_id} 未初始化。")

        await session.initialize()
        response = await session.list_tools()

        # 为每个服务器工具创建适当的工具对象
        new_tools = []
        for tool in response.tools:
            # 使用服务器ID前缀来避免工具名称冲突，用下划线替代点号
            tool_name = f"{server_id}_{tool.name}"
            
            server_tool = MCPClientTool(
                name=tool_name,
                description=f"[{server_id}] {tool.description}",
                parameters=tool.inputSchema,
                session=session,
                server_id=server_id,
                original_name=tool.name,  # 存储原始工具名
            )
            self.tool_map[tool_name] = server_tool
            new_tools.append(server_tool)

        # 更新工具列表
        self.tools = tuple(list(self.tools) + new_tools)
        logger.info(
            f"已连接到服务器 {server_id}，具有以下工具: {[tool.name for tool in response.tools]}"
        )

    async def disconnect(self, server_id: str = None) -> None:
        """
        断开与MCP服务器的连接并清理资源。
        
        参数:
            server_id: 要断开的服务器ID，如果为None则断开所有连接
        """
        if server_id is None:
            # 断开所有连接
            # 先保存服务器ID列表，以免在循环中修改字典
            server_ids = list(self.sessions.keys())
            for sid in server_ids:
                try:
                    await self.disconnect(sid)
                except Exception as e:
                    logger.error(f"断开服务器 {sid} 连接时出错: {str(e)}")
            return
            
        if server_id in self.sessions:
            try:
                # 移除该服务器的工具
                tools_to_remove = []
                for name, tool in self.tool_map.items():
                    if isinstance(tool, MCPClientTool) and tool.server_id == server_id:
                        tools_to_remove.append(name)
                        
                for name in tools_to_remove:
                    del self.tool_map[name]
                    
                # 更新工具列表，不包括要删除的服务器的工具
                self.tools = tuple(tool for tool in self.tools if 
                                not (isinstance(tool, MCPClientTool) and tool.server_id == server_id))
                
                # 保存一个本地引用，这样即使删除了字典中的引用，也能关闭这个会话和栈
                session_ref = self.sessions.get(server_id)
                exit_stack_ref = self.exit_stacks.get(server_id)
                
                # 先从字典中移除引用，防止任何后续操作继续使用这些引用
                if server_id in self.sessions:
                    del self.sessions[server_id]
                if server_id in self.exit_stacks:
                    del self.exit_stacks[server_id]
                    
                # 如果断开的是默认会话，则重置默认会话
                if self.session == session_ref:
                    if self.sessions:
                        # 随机选择一个会话作为新的默认会话
                        first_id = next(iter(self.sessions))
                        self.session = self.sessions[first_id]
                        self.exit_stack = self.exit_stacks[first_id]
                    else:
                        self.session = None
                        self.exit_stack = None
                
                # 安全关闭会话和退出栈
                if exit_stack_ref is not None:
                    logger.info(f"安全关闭服务器 {server_id} 的会话和退出栈")
                    
                    # 使用一个分离的、不受当前取消作用域影响的函数来关闭会话
                    async def close_detached():
                        try:
                            # 使用一个新的事件循环来关闭栈，避免cancel scope传播
                            def sync_close():
                                try:
                                    # 创建一个新的事件循环并在其中运行关闭操作
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    try:
                                        # 在新循环中关闭栈
                                        loop.run_until_complete(exit_stack_ref.aclose())
                                    finally:
                                        loop.close()
                                except Exception as e:
                                    # 不能在这里使用日志（可能会导致异步问题）
                                    pass
                            
                            # 在一个新线程中运行同步关闭函数
                            import threading
                            close_thread = threading.Thread(target=sync_close)
                            close_thread.daemon = True  # 设置为守护线程，不阻止程序退出
                            close_thread.start()
                            close_thread.join(timeout=2.0)  # 等待最多2秒
                        except Exception:
                            # 任何错误都被忽略
                            pass
                    
                    # 创建并立即运行关闭任务，不等待其完成
                    asyncio.create_task(close_detached())
                
                logger.info(f"已断开与MCP服务器 {server_id} 的连接")
                
            except Exception as e:
                logger.error(f"断开服务器 {server_id} 连接时出错: {str(e)}")
                # 确保从字典中移除，即使出错
                if server_id in self.sessions:
                    del self.sessions[server_id]
                if server_id in self.exit_stacks:
                    del self.exit_stacks[server_id]

    def get_server_ids(self) -> List[str]:
        """获取所有已连接服务器的ID列表"""
        return list(self.sessions.keys())
