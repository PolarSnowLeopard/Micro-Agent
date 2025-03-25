from app.tool.base import BaseTool, ToolResult
import asyncssh
from dotenv import load_dotenv
import os

load_dotenv()

_REMOTE_SSH_DESCRIPTION = """在远程服务器上执行SSH命令。"""

class RemoteSSH(BaseTool):
    name: str = "remote_ssh"
    description: str = _REMOTE_SSH_DESCRIPTION
    parameters: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要在远程服务器上执行的命令",
            },
            # 可以添加其他参数如端口、用户名等
        },
        "required": ["command"],
    }
    
    # 存储连接信息
    _connections = {}
    
    async def execute(self, command: str, **kwargs) -> ToolResult:
        try:
            # 这里需要处理SSH连接凭据，可以使用环境变量或配置文件
            server_host = os.getenv("REMOTE_SSH_SERVER")
            
            if server_host not in self._connections:
                # 从环境变量或配置中读取连接信息
                username = os.getenv("REMOTE_SSH_USERNAME")
                password = os.getenv("REMOTE_SSH_PASSWORD")
                # 建立连接
                self._connections[server_host] = await asyncssh.connect(
                    server_host, 
                    username=username,
                    password=password,
                    known_hosts=None  # 生产环境应验证known_hosts
                )
            
            # 执行远程命令
            conn = self._connections[server_host]
            result = await conn.run(command)
            return ToolResult(
                output=f"命令执行结果:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}",
                error=None if result.exit_status == 0 else f"命令返回错误代码: {result.exit_status}"
            )
            
        except Exception as e:
            return ToolResult(error=f"SSH执行错误: {str(e)}")
