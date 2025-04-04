from typing import Any, Dict, List, Optional, Tuple
import asyncio

from pydantic import Field

from app.agent.toolcall import ToolCallAgent
from app.logger import logger
from app.prompt.mcp import MULTIMEDIA_RESPONSE_PROMPT, NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.schema import AgentState, Message
from app.tool.base import ToolResult
from app.tool.mcp import MCPClients


class MCPAgent(ToolCallAgent):
    """用于与MCP(模型上下文协议)服务器交互的智能体。

    该智能体通过SSE或stdio传输连接到多个MCP服务器，
    并通过智能体的工具接口使服务器的工具可用。
    """

    name: str = "mcp_agent"
    description: str = "连接到多个MCP服务器并使用其工具的智能体。"

    system_prompt: str = SYSTEM_PROMPT
    next_step_prompt: str = NEXT_STEP_PROMPT

    # 初始化MCP工具集合
    mcp_clients: MCPClients = Field(default_factory=MCPClients)
    available_tools: MCPClients = None  # 将在initialize()中设置

    max_steps: int = 40
    default_connection_type: str = "stdio"  # 默认连接类型
    
    # 追踪连接的服务器
    connected_servers: List[str] = Field(default_factory=list)

    # 跟踪工具模式以检测变化
    tool_schemas: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    _refresh_tools_interval: int = 5  # 每N步刷新工具列表

    # 应触发终止的特殊工具名称
    special_tool_names: List[str] = Field(default_factory=lambda: ["terminate"])

    async def initialize(
        self,
        connection_type: Optional[str] = None,
        server_url: Optional[str] = None,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        server_id: Optional[str] = None,
    ) -> str:
        """初始化MCP连接。

        参数:
            connection_type: 要使用的连接类型("stdio"或"sse")
            server_url: MCP服务器的URL(用于SSE连接)
            command: 要运行的命令(用于stdio连接)
            args: 命令的参数(用于stdio连接)
            server_id: 服务器的唯一标识符

        返回:
            str: 服务器ID
        """
        # 设置默认连接类型
        if connection_type:
            self.default_connection_type = connection_type
        else:
            connection_type = self.default_connection_type
            
        # 日志输出连接信息
        logger.info(f"初始化{connection_type}连接到MCP服务器")
        
        # 对stdio连接进行参数验证和处理
        if connection_type == "stdio":
            # 如果未提供命令，使用默认Python解释器启动MCP服务器
            if not command:
                import sys
                command = sys.executable
                logger.info(f"使用默认Python解释器: {command}")
                
            # 如果未提供参数，使用默认的MCP服务器模块路径
            if not args:
                args = ["-m", "app.mcp.server"]
                logger.info(f"使用默认MCP服务器模块: {' '.join(args)}")
                
        # 对sse连接进行参数验证和处理
        elif connection_type == "sse":
            if not server_url:
                raise ValueError("SSE连接需要服务器URL")
            logger.info(f"连接到SSE服务器URL: {server_url}")
        else:
            raise ValueError(f"不支持的连接类型: {connection_type}")

        # 根据连接类型连接到MCP服务器
        if connection_type == "sse":
            server_id = await self.mcp_clients.connect_sse(server_url=server_url, server_id=server_id)
        elif connection_type == "stdio":
            server_id = await self.mcp_clients.connect_stdio(command=command, args=args or [], server_id=server_id)
        else:
            raise ValueError(f"不支持的连接类型: {connection_type}")

        # 将available_tools设置为我们的MCP实例
        self.available_tools = self.mcp_clients
        
        # 添加到已连接服务器列表
        if server_id not in self.connected_servers:
            self.connected_servers.append(server_id)

        # 存储初始工具模式
        await self._refresh_tools()

        # 添加关于可用工具的系统消息
        self._update_available_tools_message()
        
        logger.info(f"已成功连接到MCP服务器: {server_id}")
        return server_id
        
    def _update_available_tools_message(self) -> None:
        """更新系统消息中的可用工具列表"""
        tool_names = list(self.mcp_clients.tool_map.keys())
        tools_info = ", ".join(tool_names)
        
        # 添加或更新系统提示和可用工具信息
        tools_message = f"{self.system_prompt}\n\n可用的MCP工具: {tools_info}"
        
        # 检查是否已有系统消息
        if self.memory.messages and self.memory.messages[0].role == "system":
            # 更新现有的系统消息
            self.memory.messages[0].content = tools_message
        else:
            # 添加新的系统消息
            self.memory.add_message(Message.system_message(tools_message))

    async def connect_additional_server(
        self,
        connection_type: str,
        server_url: Optional[str] = None,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        server_id: Optional[str] = None,
    ) -> str:
        """连接到额外的MCP服务器。

        参数:
            connection_type: 要使用的连接类型("stdio"或"sse")
            server_url: MCP服务器的URL(用于SSE连接)
            command: 要运行的命令(用于stdio连接)
            args: 命令的参数(用于stdio连接)
            server_id: 服务器的唯一标识符

        返回:
            str: 服务器ID
        """
        # 调用initialize方法来处理连接，它会处理参数默认值
        server_id = await self.initialize(
            connection_type=connection_type,
            server_url=server_url,
            command=command,
            args=args,
            server_id=server_id
        )
        
        logger.info(f"已连接到额外的MCP服务器: {server_id}")
        return server_id
        
    async def disconnect_server(self, server_id: str) -> bool:
        """断开与指定MCP服务器的连接。

        参数:
            server_id: 要断开的服务器ID

        返回:
            bool: 断开连接是否成功
        """
        if server_id in self.connected_servers:
            await self.mcp_clients.disconnect(server_id)
            self.connected_servers.remove(server_id)
            
            # 更新工具信息
            self._update_available_tools_message()
            
            logger.info(f"已断开与MCP服务器的连接: {server_id}")
            return True
        
        logger.warning(f"尝试断开未连接的服务器: {server_id}")
        return False

    async def _refresh_tools(self) -> Tuple[List[str], List[str]]:
        """从MCP服务器刷新可用工具列表。

        返回:
            (added_tools, removed_tools)元组
        """
        # 如果没有连接到任何服务器，则返回空列表
        if not self.mcp_clients.sessions:
            return [], []

        # 获取当前所有工具
        current_tools = {}
        for server_id in self.connected_servers:
            session = self.mcp_clients.sessions.get(server_id)
            if not session:
                continue
                
            # 从服务器获取工具
            try:
                response = await session.list_tools()
                # 使用服务器ID前缀工具名，用下划线替代点号
                for tool in response.tools:
                    prefixed_name = f"{server_id}_{tool.name}"
                    current_tools[prefixed_name] = tool.inputSchema
            except Exception as e:
                logger.warning(f"从服务器 {server_id} 获取工具时出错: {str(e)}")

        # 确定添加、移除和更改的工具
        current_names = set(current_tools.keys())
        previous_names = set(self.tool_schemas.keys())

        added_tools = list(current_names - previous_names)
        removed_tools = list(previous_names - current_names)

        # 检查现有工具的模式变化
        changed_tools = []
        for name in current_names.intersection(previous_names):
            if current_tools[name] != self.tool_schemas.get(name):
                changed_tools.append(name)

        # 更新存储的模式
        self.tool_schemas = current_tools

        # 记录并通知变化
        if added_tools:
            logger.info(f"添加了MCP工具: {added_tools}")
            self.memory.add_message(
                Message.system_message(f"新可用工具: {', '.join(added_tools)}")
            )
        if removed_tools:
            logger.info(f"移除了MCP工具: {removed_tools}")
            self.memory.add_message(
                Message.system_message(
                    f"不再可用的工具: {', '.join(removed_tools)}"
                )
            )
        if changed_tools:
            logger.info(f"更改了MCP工具: {changed_tools}")

        return added_tools, removed_tools

    async def think(self) -> bool:
        """处理当前状态并决定下一步行动。"""
        # 检查是否还有可用的MCP会话
        if not self.mcp_clients.sessions:
            logger.info("所有MCP服务不可用，结束交互")
            self.state = AgentState.FINISHED
            return False

        # 定期刷新工具
        if self.current_step % self._refresh_tools_interval == 0:
            await self._refresh_tools()
            # 所有工具被移除表示关闭
            if not self.mcp_clients.tool_map:
                logger.info("所有MCP服务已关闭，结束交互")
                self.state = AgentState.FINISHED
                return False

        # 使用父类的think方法
        return await super().think()

    async def _handle_special_tool(self, name: str, result: Any, **kwargs) -> None:
        """处理特殊工具执行和状态变化"""
        # 首先使用父处理程序处理
        await super()._handle_special_tool(name, result, **kwargs)

        # 处理多媒体响应
        if isinstance(result, ToolResult) and result.base64_image:
            self.memory.add_message(
                Message.system_message(
                    MULTIMEDIA_RESPONSE_PROMPT.format(tool_name=name)
                )
            )
            
        # 特别处理terminate工具
        if name.lower() == "terminate" or name.endswith("_terminate"):
            status = kwargs.get("status", "无状态")
            logger.info(f"执行终止工具 {name}，状态：{status}")
            self.memory.add_message(
                Message.system_message(f"交互已终止: {status}")
            )
            # 立即标记为已完成，确保清理过程能够正确执行
            self.state = AgentState.FINISHED

    def _should_finish_execution(self, name: str, **kwargs) -> bool:
        """确定工具执行是否应该结束智能体"""
        # 如果工具名称是'terminate'则终止（无论是原始名称还是带服务器ID前缀的名称）
        if name.lower() == "terminate" or name.endswith("_terminate"):
            logger.info(f"接收到终止命令: {name}，准备结束会话")
            self.state = AgentState.FINISHED
            return True
        return False

    async def cleanup(self) -> None:
        """完成后清理所有MCP连接。"""
        # 如果没有连接服务器，则无需清理
        if not hasattr(self, 'connected_servers') or not self.connected_servers:
            return
            
        try:
            # 先保存引用，这样即使在断开连接过程中出错，也能清空connected_servers
            servers_to_disconnect = list(self.connected_servers)
            server_count = len(servers_to_disconnect)
            
            # 防止重复清理 - 先清空连接列表
            self.connected_servers = []
            
            if not servers_to_disconnect or not hasattr(self, 'mcp_clients') or not self.mcp_clients:
                return
                
            logger.info(f"开始安全清理与 {server_count} 个服务器的连接")
            
            # 创建一个新的任务来断开连接，不等待其完成
            async def detached_cleanup():
                try:
                    for server_id in servers_to_disconnect:
                        try:
                            logger.info(f"正在断开服务器 {server_id} 的连接")
                            
                            # 直接调用MCPClients的disconnect方法
                            if hasattr(self.mcp_clients, 'disconnect'):
                                await self.mcp_clients.disconnect(server_id)
                                
                            logger.info(f"已安全断开服务器 {server_id} 的连接")
                        except Exception as e:
                            logger.error(f"断开服务器 {server_id} 连接时出错，但继续处理其他服务器: {str(e)}")
                            # 继续处理其他服务器
                            continue
                except Exception as e:
                    logger.error(f"清理过程中发生全局错误: {str(e)}")
            
            # 创建清理任务但不等待其完成，让主程序可以继续执行
            asyncio.create_task(detached_cleanup())
            
            # 给清理任务一点时间开始处理
            await asyncio.sleep(0.1)
            
            logger.info("清理过程已开始，程序可以安全退出")
        except Exception as e:
            logger.error(f"初始化清理过程时出错: {str(e)}")
            # 确保列表被清空
            self.connected_servers = []

    async def run(self, request: Optional[str] = None) -> List[str]:
        """运行智能体并在完成后进行清理。"""
        cleanup_attempted = False
        try:
            # 强制刷新工具列表，确保所有工具都是最新的
            await self._refresh_tools()
            # 更新工具消息
            self._update_available_tools_message()
            
            # 运行智能体
            result = await super().run(request)
            
            # 检查运行后的状态，如果已经完成则记录
            if self.state == AgentState.FINISHED:
                logger.info("智能体已完成执行，状态为FINISHED")
            
            return result
        except Exception as e:
            logger.error(f"运行智能体时出错: {str(e)}")
            # 在异常情况下也应该标记为已完成
            self.state = AgentState.FINISHED
            raise
        finally:
            # 确保即使出现错误也只尝试清理一次
            if not cleanup_attempted:
                cleanup_attempted = True
                try:
                    # 直接调用cleanup方法但不等待其完成
                    # 这里不使用wait_for和shield，因为它们会导致cancel scope问题
                    logger.info("开始后台清理资源...")
                    
                    # 使用一个分离的、完全独立的函数来启动清理
                    async def run_detached_cleanup():
                        try:
                            await self.cleanup()
                        except Exception as e:
                            logger.error(f"分离的清理过程中出错: {str(e)}")
                    
                    # 创建任务但不等待其完成
                    asyncio.create_task(run_detached_cleanup())
                    
                    # 确保不会有任何等待操作，直接返回
                    logger.info("清理任务已在后台启动，主程序可以继续执行")
                except Exception as cleanup_error:
                    logger.error(f"启动清理任务时发生错误: {str(cleanup_error)}")
                    # 错误已记录，但不重新抛出，让程序能继续执行
