#!/usr/bin/env python
import argparse
import asyncio
import sys
import json
from datetime import datetime
from app.agent.mcp import MCPAgent
from app.config import config
from app.logger import logger
from app.prompt.task import (
    CODE_ANALYSIS_PROMPT,
    SERVICE_PACKAGING_PROMPT,
    REMOTE_DEPLOY_PROMPT
)
from app.utils.visualize_record import save_record_to_json, generate_visualization_html

class MCPRunner:
    """MCP智能体运行器类，具有适当的路径处理和配置。"""

    def __init__(self, agent_name: str = "Micro-Agent"):
        self.root_path = config.root_path
        self.server_reference = "app.mcp.server"
        self.agent = MCPAgent(name=agent_name)
        self.connection_type = "stdio"
        self.server_url = None

    async def initialize(
        self,
        connection_type: str,
        server_url: str | None = None,
    ) -> None:
        """使用适当的连接方式初始化MCP智能体。"""
        logger.info(f"使用{connection_type}连接初始化MCPAgent...")
        
        # 保存连接参数以便后续重连
        self.connection_type = connection_type
        self.server_url = server_url

        if connection_type == "stdio":
            await self.agent.initialize(
                connection_type="stdio",
                command=sys.executable,
                args=["-m", self.server_reference],
            )
        else:  # sse
            await self.agent.initialize(connection_type="sse", server_url=server_url)

        logger.info(f"通过{connection_type}连接到MCP服务器")

    async def ensure_connection(self) -> bool:
        """确保MCP连接可用，如果断开则重新连接。
        
        返回:
            bool: 连接是否可用
        """
        # 检查Agent的连接状态
        if (hasattr(self.agent, 'mcp_clients') and 
            (not self.agent.mcp_clients.session or not self.agent.mcp_clients.tool_map)):
            logger.info("检测到MCP连接不可用，正在尝试重新连接...")
            
            # 创建新的Agent实例
            self.agent = MCPAgent()
            
            # 重新初始化
            try:
                await self.initialize(
                    connection_type=self.connection_type, 
                    server_url=self.server_url
                )
                
                # 检查连接是否恢复
                if (not hasattr(self.agent, 'mcp_clients') or 
                    not self.agent.mcp_clients.session or 
                    not self.agent.mcp_clients.tool_map):
                    logger.error("无法重新建立MCP连接")
                    return False
                    
                logger.info("MCP连接已重新建立")
                return True
            except Exception as e:
                logger.error(f"重新连接时出错: {str(e)}")
                return False
        
        return True

    async def run_interactive(self) -> None:
        """以交互模式运行智能体。"""
        print("\nMCP智能体交互模式（输入'exit'退出）\n")
        while True:
            user_input = input("\n请输入您的请求: ")
            if user_input.lower() in ["exit", "quit", "q"]:
                break
                
            # 确保连接可用
            if not await self.ensure_connection():
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
        await self.agent.cleanup()
        logger.info("会话已结束")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="运行MCP智能体")
    parser.add_argument(
        "--connection",
        "-c",
        choices=["stdio", "sse"],
        default="stdio",
        help="连接类型：stdio或sse",
    )
    parser.add_argument(
        "--server-url",
        default="http://127.0.0.1:8000/sse",
        help="SSE连接的URL",
    )
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
        await runner.initialize(args.connection, args.server_url)

        if args.prompt:
            await runner.run_single_prompt(args.prompt)
        elif args.interactive:
            await runner.run_interactive()
        elif args.subtask:
            await runner.run_subtask(args.subtask)
        else:
            await runner.run_default()

    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"运行MCPAgent时出错: {str(e)}", exc_info=True)
        sys.exit(1)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run_mcp())
