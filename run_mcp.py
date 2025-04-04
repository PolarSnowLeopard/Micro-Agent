#!/usr/bin/env python
import argparse
import asyncio
import sys
import json
from datetime import datetime
from app.agent.mcp import MCPAgent
from app.config import config
from app.logger import logger
from app.task.code_analysis_task import (
    CODE_ANALYSIS_PROMPT,
    SERVICE_PACKAGING_PROMPT,
    REMOTE_DEPLOY_PROMPT
)
from app.utils.visualize_record import save_record_to_json, generate_visualization_html
from app.schema import AgentState
from app.tool.mcp import MCPClientTool

class MCPRunner:
    """MCP智能体运行器类，具有适当的路径处理和配置。"""

    def __init__(self, agent_name: str = "Micro-Agent"):
        self.root_path = config.root_path
        self.server_reference = "app.mcp.server"
        self.agent = MCPAgent(name=agent_name)
        # 存储服务器配置，支持多个服务器连接
        self.server_configs = []

    async def add_server(
        self,
        connection_type: str,
        server_url: str = None,
        server_id: str = None,
        command: str = None,
        args: list = None,
    ) -> str:
        """添加并连接到一个MCP服务器。
        
        参数:
            connection_type: 连接类型 ("stdio" 或 "sse")
            server_url: SSE服务器的URL (用于sse连接)
            server_id: 服务器唯一标识符，如果为None则自动生成
            command: 执行的命令 (用于stdio连接)
            args: 命令参数 (用于stdio连接)
            
        返回:
            str: 服务器ID
        """
        logger.info(f"使用{connection_type}连接初始化MCPAgent服务器...")
        
        # 设置默认值 - 使用内置的MCP服务器模块
        if connection_type == "stdio" and not command:
            # 使用当前Python解释器启动内置MCP服务器
            command = sys.executable
            if not args:
                args = ["-m", self.server_reference]
            logger.info(f"未指定命令，使用默认内置MCP服务器: {command} {' '.join(args)}")
        elif connection_type == "sse" and not server_url:
            # 如果是SSE连接但未指定URL，则报错
            raise ValueError("SSE连接需要指定server_url")
        
        # 连接到服务器
        if len(self.server_configs) == 0:
            # 第一次连接使用initialize方法
            server_id = await self.agent.initialize(
                connection_type=connection_type,
                server_url=server_url,
                command=command,
                args=args,
                server_id=server_id
            )
        else:
            # 后续连接使用connect_additional_server方法
            server_id = await self.agent.connect_additional_server(
                connection_type=connection_type,
                server_url=server_url,
                command=command,
                args=args,
                server_id=server_id
            )
            
        # 保存服务器配置以便后续重连
        self.server_configs.append({
            "server_id": server_id,
            "connection_type": connection_type,
            "server_url": server_url,
            "command": command,
            "args": args
        })
        
        logger.info(f"通过{connection_type}连接到MCP服务器 {server_id}")
        return server_id

    async def ensure_connections(self) -> bool:
        """确保所有MCP连接可用，如果断开则重新连接。
        
        返回:
            bool: 是否至少有一个连接可用
        """
        try:
            # 检查是否有任何连接
            if not hasattr(self.agent, 'mcp_clients') or not self.agent.mcp_clients.sessions:
                logger.info("检测到MCP连接不可用，正在尝试重新连接...")
                
                # 如果现有agent有cleanup方法，尝试调用它清理资源
                if hasattr(self.agent, 'cleanup'):
                    try:
                        await self.agent.cleanup()
                    except Exception as e:
                        logger.warning(f"清理旧Agent资源时出错: {str(e)}")
                
                # 创建新的Agent实例
                agent_name = self.agent.name
                self.agent = MCPAgent(name=agent_name)
                
                # 重新连接所有配置的服务器
                successful_connections = 0
                for config in self.server_configs:
                    try:
                        await self.add_server(
                            connection_type=config["connection_type"],
                            server_url=config["server_url"],
                            server_id=config["server_id"],
                            command=config["command"],
                            args=config["args"]
                        )
                        successful_connections += 1
                    except Exception as e:
                        logger.error(f"重新连接到服务器 {config.get('server_id', '未知')} 时出错: {str(e)}")
                
                # 检查是否至少有一个连接恢复
                if successful_connections == 0:
                    logger.error("无法重新建立任何MCP连接")
                    return False
                    
                logger.info(f"已重新建立 {successful_connections} 个MCP连接")
                return True
            
            return True
        except Exception as e:
            logger.error(f"检查连接状态时出错: {str(e)}")
            return False

    async def run_interactive(self) -> None:
        """以交互模式运行智能体。"""
        print("\nMCP智能体交互模式（输入'exit'退出）\n")
        
        # 打印当前连接的服务器列表和可用工具
        if self.agent.connected_servers:
            print(f"已连接的服务器: {', '.join(self.agent.connected_servers)}")
            
            # 显示每个服务器上可用的工具
            print("\n可用工具列表:")
            for server_id in self.agent.connected_servers:
                tools = [name for name, tool in self.agent.mcp_clients.tool_map.items() 
                         if isinstance(tool, MCPClientTool) and tool.server_id == server_id]
                if tools:
                    print(f"  服务器 {server_id}:")
                    for tool_name in tools:
                        # 获取原始工具名（不带前缀）
                        original_name = self.agent.mcp_clients.tool_map[tool_name].original_name
                        print(f"    - {tool_name} (原始名称: {original_name})")
            
            print("\n使用工具时，请使用完整的工具名（包含服务器前缀），例如: 'stdio_0_cmd' 或 'sse_1_calculate_linezolid_dose'\n")
            
        while True:
            user_input = input("\n请输入您的请求: ")
            if user_input.lower() in ["exit", "quit", "q"]:
                break
                
            # 确保连接可用
            if not await self.ensure_connections():
                print("无法连接到MCP服务器，请检查服务器状态或重启程序")
                continue
                
            try:
                result = await self.agent.run(user_input)
                result = json.dumps(result, ensure_ascii=False, indent=4)
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                save_record_to_json(f"record_{timestamp}", result)
                generate_visualization_html(f"record_{timestamp}")
            except Exception as e:
                logger.error(f"执行请求时出错: {str(e)}")
                print(f"\n执行出错: {str(e)}")

    async def run_single_prompt(self, prompt: str) -> str:
        """使用单个提示运行智能体，并保存结果。"""
        result = await self.agent.run(prompt)
        result = json.dumps(result, ensure_ascii=False, indent=4)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        save_record_to_json(f"record_{timestamp}", result)
        generate_visualization_html(f"record_{timestamp}")
        return result

    async def run_subtask(self, subtask: str) -> None:
        """使用单个提示子任务运行智能体。"""
        if subtask == "1":
            result = await self.agent.run(CODE_ANALYSIS_PROMPT)
        elif subtask == "2":
            result = await self.agent.run(SERVICE_PACKAGING_PROMPT)
        elif subtask == "3":
            result = await self.agent.run(REMOTE_DEPLOY_PROMPT)


    async def run_default(self) -> None:
        """以默认模式运行智能体。"""
        result = await self.agent.run(
            "你好，我可以使用哪些工具？列出工具后请终止。"
        )

    async def cleanup(self) -> None:
        """清理智能体资源。"""
        try:
            # 创建一个分离的任务来执行清理，无需等待其完成
            logger.info("正在清理资源")
            
            async def detached_cleanup():
                try:
                    # 尝试调用agent的cleanup
                    if hasattr(self.agent, 'cleanup'):
                        await self.agent.cleanup()
                except Exception as e:
                    logger.error(f"代理清理过程中出错: {str(e)}")
            
            # 创建任务但不等待其完成
            asyncio.create_task(detached_cleanup())
            
            # 记录清理开始信息
            logger.info("会话已结束，清理过程已在后台启动")
        except Exception as e:
            logger.error(f"启动清理过程时出错: {str(e)}")
            # 即使清理失败，也不影响主程序退出

    async def run_stream(self, prompt: str):
        """
        以流式方式运行智能体，每完成一个步骤就yield结果

        参数:
            prompt: 任务的prompt
        
        返回:
            异步生成器，每次返回单个步骤的结果
        """
        if not await self.ensure_connections():
            yield {"error": "无法连接到MCP服务器，请检查服务器状态或重启程序"}
            return

        try:
            # 初始化运行状态
            self.agent.current_step = 0
            self.agent.state = AgentState.IDLE
            
            if prompt:
                self.agent.update_memory("user", prompt)
            
            # 使用状态上下文运行agent
            async with self.agent.state_context(AgentState.RUNNING):
                while (
                    self.agent.current_step < self.agent.max_steps and 
                    self.agent.state != AgentState.FINISHED
                ):
                    self.agent.current_step += 1
                    logger.info(f"执行步骤 {self.agent.current_step}/{self.agent.max_steps}")
                    
                    # 执行单个步骤
                    step_result = await self.agent.step()
                    step_result["step"] = self.agent.current_step
                    
                    # 检查是否陷入循环
                    if self.agent.is_stuck():
                        self.agent.handle_stuck_state()
                    
                    # 将当前步骤结果以JSON格式返回
                    yield step_result
                
                # 达到最大步骤或完成任务
                if self.agent.current_step >= self.agent.max_steps:
                    self.agent.current_step = 0
                    self.agent.state = AgentState.IDLE
                    yield {
                        "thought": f"已终止: 达到最大步骤数 ({self.agent.max_steps})",
                        "step": str(self.agent.current_step),
                        "is_last": True
                    }
                else:
                    # 标记最后一步
                    yield {"is_last": True}
                    
        except Exception as e:
            logger.error(f"运行MCPAgent时出错: {str(e)}", exc_info=True)
            yield {"error": f"运行出错: {str(e)}", "is_last": True}


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="运行MCP智能体")
    # 默认服务器配置（主要服务器，通常是stdio）
    parser.add_argument(
        "--connection",
        "-c",
        choices=["stdio", "sse"],
        default="stdio",
        help="主服务器连接类型：stdio或sse",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        help="主服务器的SSE连接URL",
    )
    parser.add_argument(
        "--server-id",
        default=None,
        help="主服务器的唯一标识符",
    )
    
    # 额外的SSE服务器配置
    parser.add_argument(
        "--sse-servers",
        nargs="+",
        default=[],
        help="附加的SSE服务器URL列表，格式: [url1 url2 ...]",
    )
    parser.add_argument(
        "--sse-ids",
        nargs="+",
        default=[],
        help="附加的SSE服务器ID列表，格式: [id1 id2 ...]，与--sse-servers一一对应",
    )
    
    # 额外的stdio服务器配置
    parser.add_argument(
        "--stdio-servers",
        nargs="+",
        default=[],
        help="附加的stdio服务器命令列表，格式: [command1 command2 ...]",
    )
    parser.add_argument(
        "--stdio-args",
        nargs="+",
        action="append",
        default=[],
        help="附加的stdio服务器参数列表，格式: [arg1 arg2 ...] [arg3 arg4 ...] ...",
    )
    parser.add_argument(
        "--stdio-ids",
        nargs="+",
        default=[],
        help="附加的stdio服务器ID列表，格式: [id1 id2 ...]，与--stdio-servers一一对应",
    )
    
    # 运行模式
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="以交互模式运行"
    )
    parser.add_argument("--prompt", "-p", help="执行单个提示并退出")
    parser.add_argument("--subtask", "-s", choices=["1", "2", "3", ""], 
                        default="", help="执行子任务并退出")
    return parser.parse_args()


async def run_mcp() -> None:
    """MCP运行器的主入口点。"""
    args = parse_args()
    runner = MCPRunner()
    
    try:
        # 首先添加内置MCP服务器
        built_in_server_id = None
        try:
            logger.info("添加默认内置MCP服务器")
            built_in_server_id = await runner.add_server(
                connection_type="stdio",
                server_url=None,
                command=None,  # 使用默认Python解释器
                args=None,     # 使用默认的app.mcp.server模块
                server_id="stdio_built_in"  # 指定一个固定ID以便识别
            )
            logger.info(f"成功添加内置MCP服务器: {built_in_server_id}")
        except Exception as e:
            logger.error(f"添加内置MCP服务器时出错: {str(e)}")
            # 如果内置服务器失败，仍然尝试用户指定的服务器
        
        # 连接到主服务器（如果与内置服务器不同）
        if args.connection != "stdio" or args.server_url or args.server_id:
            try:
                await runner.add_server(
                    connection_type=args.connection,
                    server_url=args.server_url,
                    server_id=args.server_id
                )
            except Exception as e:
                logger.error(f"连接到主服务器时出错: {str(e)}")
                # 如果至少有内置服务器，则不需要中断程序
                if not built_in_server_id:
                    raise RuntimeError("无法连接到任何MCP服务器，程序无法继续执行") from e
        
        # 连接到额外的SSE服务器
        if args.sse_servers:
            for i, url in enumerate(args.sse_servers):
                try:
                    server_id = args.sse_ids[i] if i < len(args.sse_ids) else None
                    await runner.add_server(
                        connection_type="sse",
                        server_url=url,
                        server_id=server_id
                    )
                except Exception as e:
                    logger.error(f"连接到额外的SSE服务器 {url} 时出错: {str(e)}")
                    # 继续连接其他服务器，不中断整个程序
            
        # 连接到额外的stdio服务器
        if args.stdio_servers:
            for i, command in enumerate(args.stdio_servers):
                try:
                    server_id = args.stdio_ids[i] if i < len(args.stdio_ids) else None
                    cmd_args = args.stdio_args[i] if i < len(args.stdio_args) else None
                    await runner.add_server(
                        connection_type="stdio",
                        command=command,
                        args=cmd_args,
                        server_id=server_id
                    )
                except Exception as e:
                    logger.error(f"连接到额外的stdio服务器 {command} 时出错: {str(e)}")
                    # 继续连接其他服务器，不中断整个程序

        # 根据参数执行不同的操作
        try:
            if args.prompt:
                await runner.run_single_prompt(args.prompt)
            elif args.interactive:
                await runner.run_interactive()
            elif args.subtask:
                await runner.run_subtask(args.subtask)
            else:
                await runner.run_default()
        except Exception as e:
            logger.error(f"执行操作时出错: {str(e)}")
            # 即使执行操作出错，我们也继续尝试清理资源

    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"运行MCPAgent时出错: {str(e)}", exc_info=True)
    finally:
        # 确保在程序退出前尝试清理资源，但不等待其完成
        # 这里使用不会抛出异常的方式调用cleanup
        try:
            # 创建分离的清理任务但不等待它
            logger.info("程序退出前执行最终清理")
            asyncio.create_task(runner.cleanup())
            # 给清理任务一点启动时间，但不等待它完成
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"最终清理时出错: {str(e)}")
            # 继续退出程序


if __name__ == "__main__":
    asyncio.run(run_mcp())
