import asyncio
import json
import os
import sys
import logging
from typing import Optional, Dict, List, Any
from contextlib import AsyncExitStack
import time
from mcp import ClientSession
from mcp.client.sse import sse_client
from openai import AsyncOpenAI
from dotenv import load_dotenv

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# 加载环境变量
load_dotenv()  # 从.env文件加载环境变量
class Configuration:
    """管理MCP客户端的配置和环境变量"""
    
    def __init__(self):
        """初始化配置"""
        self.openai_api_key = os.getenv("OPENAI_API_KEY_")
        self.openai_base_url = os.getenv("OPENAI_BASE_URL")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
        self.max_tokens = int(os.getenv("MAX_TOKENS", "1000"))
        
    @staticmethod
    def load_config(file_path: str) -> dict:
        """从JSON文件加载服务器配置
        
        参数:
            file_path: JSON配置文件路径
            
        返回:
            包含服务器配置的字典
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logging.error(f"配置文件未找到: {file_path}")
            return {"servers": []}
        except json.JSONDecodeError:
            logging.error(f"配置文件格式无效: {file_path}")
            return {"servers": []}

class ServerConnection:
    """管理与MCP服务器的连接"""
    
    def __init__(self, name: str, url: str):
        """初始化服务器连接
        
        参数:
            name: 服务器名称
            url: 服务器URL
        """
        self.name = name
        self.url = url
        self.session: Optional[ClientSession] = None
        self._streams_context = None
        self._session_context = None
        self._cleanup_lock = asyncio.Lock()
        self.exit_stack = AsyncExitStack()
        
    async def initialize(self) -> None:
        """初始化与服务器的连接"""
        try:
            logging.info(f"正在连接到服务器 {self.name} ({self.url})...")
            self._streams_context = sse_client(url=self.url)
            streams = await self._streams_context.__aenter__()
            self._session_context = ClientSession(*streams)
            self.session = await self._session_context.__aenter__()
            
            # 初始化
            await self.session.initialize()
            
            # 列出可用工具以验证连接
            response = await self.session.list_tools()
            tools = response.tools
            logging.info(f"已连接到服务器 {self.name}，可用工具: {[tool.name for tool in tools]}")
            return tools
        except Exception as e:
            logging.error(f"连接到服务器 {self.name} 失败: {str(e)}")
            await self.cleanup()
            raise
    
    async def list_tools(self) -> Any:
        """获取服务器上可用的工具列表"""
        if not self.session:
            raise RuntimeError(f"服务器 {self.name} 未初始化")
        
        response = await self.session.list_tools()
        return response.tools
    
    async def call_tool(self, tool_name: str, arguments: dict, retries: int = 2, delay: float = 1.0) -> Any:
        """调用服务器上的工具
        
        参数:
            tool_name: 工具名称
            arguments: 工具参数
            retries: 重试次数
            delay: 重试延迟(秒)
            
        返回:
            工具执行结果
        """
        if not self.session:
            raise RuntimeError(f"服务器 {self.name} 未初始化")
        
        attempt = 0
        while attempt < retries:
            try:
                logging.info(f"正在执行工具 {tool_name}...")
                result = await self.session.call_tool(tool_name, arguments)
                return result
            except Exception as e:
                attempt += 1
                logging.warning(f"执行工具出错: {str(e)}. 尝试 {attempt}/{retries}")
                if attempt < retries:
                    logging.info(f"{delay}秒后重试...")
                    await asyncio.sleep(delay)
                else:
                    logging.error("达到最大重试次数，失败。")
                    raise
    
    async def cleanup(self) -> None:
        """清理服务器资源"""
        async with self._cleanup_lock:
            try:
                if self._session_context:
                    await self._session_context.__aexit__(None, None, None)
                if self._streams_context:
                    await self._streams_context.__aexit__(None, None, None)
                self.session = None
                self._session_context = None
                self._streams_context = None
            except Exception as e:
                logging.error(f"清理服务器 {self.name} 资源时出错: {str(e)}")

class MCPClient:
    """MCP客户端，支持连接多个服务器"""
    
    def __init__(self, config_path: str = None):
        """初始化MCP客户端
        
        参数:
            config_path: 配置文件路径，如果为None则使用命令行参数
        """
        self.config = Configuration()
        self.servers: Dict[str, ServerConnection] = {}
        self.openai = AsyncOpenAI(
            api_key=self.config.openai_api_key, 
            base_url=self.config.openai_base_url
        )
        self.config_path = config_path
    
    async def load_servers_from_config(self, config_path: str) -> None:
        """从配置文件加载并初始化服务器
        
        参数:
            config_path: 配置文件路径
        """
        server_config = self.config.load_config(config_path)
        init_tasks = []
        
        if "servers" in server_config:
            for server_info in server_config["servers"]:
                name = server_info.get("name", f"server_{len(self.servers)}")
                url = server_info.get("url")
                if url:
                    server = ServerConnection(name, url)
                    # 创建初始化任务
                    init_tasks.append(self._init_server(name, server))
        
        # 并行等待所有服务器初始化
        if init_tasks:
            await asyncio.gather(*init_tasks, return_exceptions=True)
    
    async def _init_server(self, name: str, server: ServerConnection) -> None:
        """初始化服务器并添加到服务器列表
        
        参数:
            name: 服务器名称
            server: 服务器连接对象
        """
        try:
            await server.initialize()
            self.servers[name] = server
        except Exception as e:
            logging.error(f"初始化服务器 {name} ({server.url}) 失败: {str(e)}")
            # 不添加到服务器列表
    
    async def add_server(self, name: str, url: str) -> None:
        """添加并初始化服务器
        
        参数:
            name: 服务器名称
            url: 服务器URL
        """
        server = ServerConnection(name, url)
        try:
            await server.initialize()
            self.servers[name] = server
            logging.info(f"成功添加服务器 {name}")
        except Exception as e:
            logging.error(f"添加服务器 {name} 失败: {str(e)}")
    
    async def initialize(self) -> None:
        """初始化客户端并加载所有服务器"""
        if self.config_path:
            await self.load_servers_from_config(self.config_path)
    
    async def cleanup(self) -> None:
        """清理所有服务器连接"""
        cleanup_tasks = []
        for server in self.servers.values():
            cleanup_tasks.append(asyncio.create_task(server.cleanup()))
        
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    
    async def get_all_tools(self) -> List[Any]:
        """获取所有服务器上的工具"""
        all_tools = []
        for server_name, server in self.servers.items():
            try:
                tools = await server.list_tools()
                for tool in tools:
                    tool_info = {
                        "name": tool.name,
                        "description": tool.description,
                        "server": server_name,
                        "schema": tool.inputSchema
                    }
                    all_tools.append(tool_info)
            except Exception as e:
                logging.error(f"获取服务器 {server_name} 工具失败: {str(e)}")
        
        return all_tools
    
    async def process_query(self, query: str) -> str:
        """使用OpenAI API和可用工具处理查询
        
        参数:
            query: 用户查询
            
        返回:
            处理结果
        """
        messages = [
            {
                "role": "user",
                "content": query
            }
        ]
        
        all_tools = await self.get_all_tools()
        
        if not all_tools:
            return "错误: 没有可用的工具。请确保至少有一个服务器已成功连接。"
        
        available_tools = [{ 
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["schema"]
            }
        } for tool in all_tools]
        
        # 初始OpenAI API调用
        completion = await self.openai.chat.completions.create(
            model=self.config.openai_model,
            max_tokens=self.config.max_tokens,
            messages=messages,
            tools=available_tools
        )
        
        # 处理响应和工具调用
        tool_results = []
        final_text = []
        
        assistant_message = completion.choices[0].message
        
        if assistant_message.tool_calls:
            for tool_call in assistant_message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)
                
                # 查找哪个服务器有这个工具
                server_with_tool = None
                for tool_info in all_tools:
                    if tool_info["name"] == tool_name:
                        server_with_tool = self.servers[tool_info["server"]]
                        break
                
                if not server_with_tool:
                    error_msg = f"找不到提供工具 {tool_name} 的服务器"
                    logging.error(error_msg)
                    final_text.append(f"[错误: {error_msg}]")
                    continue
                
                # 执行工具调用
                try:
                    result = await server_with_tool.call_tool(tool_name, tool_args)
                    tool_results.append({"call": tool_name, "result": result})
                    final_text.append(f"[调用工具 {tool_name}，参数 {tool_args}]")
                    
                    # 用工具结果继续对话
                    messages.extend([
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [tool_call]
                        },
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result.content[0].text
                        }
                    ])
                    
                    logging.info(f"工具 {tool_name} 返回结果: {result.content[0].text}")
                    
                    # 从OpenAI获取下一个响应
                    completion = await self.openai.chat.completions.create(
                        model=self.config.openai_model,
                        max_tokens=self.config.max_tokens,
                        messages=messages,
                    )
                    
                    if isinstance(completion.choices[0].message.content, (dict, list)):
                        final_text.append(str(completion.choices[0].message.content))
                    else:
                        final_text.append(completion.choices[0].message.content)
                except Exception as e:
                    error_msg = f"执行工具 {tool_name} 失败: {str(e)}"
                    logging.error(error_msg)
                    final_text.append(f"[错误: {error_msg}]")
        else:
            if isinstance(assistant_message.content, (dict, list)):
                final_text.append(str(assistant_message.content))
            else:
                final_text.append(assistant_message.content)
                
        return "\n".join(final_text)
    
    async def chat_loop(self) -> None:
        """运行交互式聊天循环"""
        print("\nMCP客户端已启动！")
        
        # 检查服务器状态
        if not self.servers:
            print("警告: 没有连接到任何服务器!")
        else:
            print("连接到以下服务器:")
            for name, server in self.servers.items():
                status = "已初始化" if server.session else "未初始化"
                print(f"- {name}: {status}")
        
        print("\n输入查询或输入'quit'退出。")
        
        while True:
            try:
                query = input("\n查询: ").strip()
                
                if query.lower() == 'quit':
                    break
                
                response = await self.process_query(query)
                print("\n" + response)
                
            except Exception as e:
                print(f"\n错误: {str(e)}")
                logging.error(f"处理查询时出错: {str(e)}", exc_info=True)

async def main() -> None:
    """主函数"""
    # 检查是否提供了配置文件
    config_file = None
    server_urls = []
    
    if len(sys.argv) > 1:
        # 检查第一个参数是否是配置文件
        if sys.argv[1].endswith('.json'):
            config_file = sys.argv[1]
        else:
            # 否则假设所有参数都是服务器URL
            server_urls = sys.argv[1:]
    
    client = MCPClient(config_file)
    
    # 初始化客户端(从配置文件加载服务器)
    await client.initialize()
    
    # 如果提供了URL作为命令行参数，则添加这些服务器
    for i, url in enumerate(server_urls):
        await client.add_server(f"server_{i}", url)
    
    # 如果没有服务器，则退出
    if not client.servers:
        print("错误: 未配置服务器。请提供配置文件或服务器URL。")
        print("用法: python mcp_client.py [config.json | server_url1 server_url2 ...]")
        return
    
    try:
        await client.chat_loop()
    finally:
        await client.cleanup()

if __name__ == "__main__":
    asyncio.run(main())

    # python dev_mcp/mcp_client.py http://localhost:8800/sse