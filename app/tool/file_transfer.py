from app.tool.base import BaseTool, ToolResult
import os
import platform
from pathlib import Path
import base64
import tempfile
import asyncio
from app.tool.remote_ssh import RemoteSSH
from app.logger import logger

class FileTransfer(BaseTool):
    name: str = "file_transfer"
    description: str = "将文件或目录传输到远程服务器，支持Windows和Linux平台"
    parameters: dict = {
        "type": "object",
        "properties": {
            "local_path": {
                "type": "string",
                "description": "本地文件或目录路径",
            },
            "remote_path": {
                "type": "string",
                "description": "远程服务器上的目标路径（不包含主机信息，仅为目录/文件名）",
            }
        },
        "required": ["local_path", "remote_path"],
    }
    
    async def execute(self, local_path: str, remote_path: str, **kwargs) -> ToolResult:
        # 检查本地路径是否存在
        if not os.path.exists(local_path):
            return ToolResult(error=f"本地路径不存在: {local_path}")
        
        # 标准化路径（处理不同操作系统的路径分隔符）
        local_path = str(Path(local_path))
        
        # 检测操作系统类型
        system = platform.system()
        logger.info(f"检测到操作系统: {system}")
        
        # 从环境变量获取远程主机信息
        remote_server = os.environ.get("REMOTE_SSH_SERVER", "")
        remote_username = os.environ.get("REMOTE_SSH_USERNAME", "")
        remote_password = os.environ.get("REMOTE_SSH_PASSWORD", "")
        
        if not remote_server:
            return ToolResult(error="未找到远程服务器信息，请确保REMOTE_SSH_SERVER环境变量已设置")
        
        # 构建完整的远程路径
        full_remote_path = f"{remote_username}@{remote_server}:{remote_path}" if remote_username else f"{remote_server}:{remote_path}"
        logger.info(f"构建的完整远程路径: {full_remote_path}")
        
        ssh_tool = RemoteSSH()
        
        # 检查是文件还是目录
        is_directory = os.path.isdir(local_path)
        
        if system == "Linux" or system == "Darwin":
            # Linux或MacOS使用scp命令
            if is_directory:
                # 对于目录，递归复制
                cmd = f"scp -r \"{local_path}\" \"{full_remote_path}\""
            else:
                # 对于文件，直接复制
                cmd = f"scp \"{local_path}\" \"{full_remote_path}\""
            
            # 使用本地的操作系统命令执行scp
            try:
                import subprocess
                process = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    return ToolResult(error=f"SCP传输失败: {stderr.decode()}")
                return ToolResult(output=f"文件传输成功: {local_path} -> {full_remote_path}")
            except Exception as e:
                return ToolResult(error=f"执行SCP命令时出错: {str(e)}")
            
        elif system == "Windows":
            # Windows平台的处理方法
            if is_directory:
                # 目录传输策略
                # 1. 创建远程目录
                mkdir_cmd = f"mkdir -p {remote_path}"
                mkdir_result = await ssh_tool.execute(mkdir_cmd)
                if mkdir_result.error:
                    return ToolResult(error=f"创建远程目录失败: {mkdir_result.error}")
                
                # 2. 遍历目录中的所有文件并逐个传输
                result_messages = []
                for root, dirs, files in os.walk(local_path):
                    # 创建相对路径
                    rel_path = os.path.relpath(root, local_path)
                    if rel_path != '.':
                        remote_dir = os.path.join(remote_path, rel_path).replace('\\', '/')
                        mkdir_cmd = f"mkdir -p {remote_dir}"
                        mkdir_result = await ssh_tool.execute(mkdir_cmd)
                        if mkdir_result.error:
                            result_messages.append(f"创建远程子目录失败 {remote_dir}: {mkdir_result.error}")
                            continue
                    
                    # 传输文件
                    for file in files:
                        local_file = os.path.join(root, file)
                        if rel_path == '.':
                            remote_file = os.path.join(remote_path, file).replace('\\', '/')
                        else:
                            remote_file = os.path.join(remote_path, rel_path, file).replace('\\', '/')
                        
                        # 单文件传输
                        transfer_result = await self._transfer_single_file(local_file, remote_file, ssh_tool)
                        result_messages.append(transfer_result)
                
                return ToolResult(output="\n".join(result_messages))
            else:
                # 单个文件传输
                result = await self._transfer_single_file(local_path, remote_path, ssh_tool)
                return ToolResult(output=result)
        else:
            return ToolResult(error=f"不支持的操作系统: {system}")
    
    async def _transfer_single_file(self, local_file: str, remote_file: str, ssh_tool: RemoteSSH) -> str:
        """使用base64编码方式传输单个文件"""
        try:
            # 读取文件内容并base64编码
            with open(local_file, 'rb') as f:
                file_content = f.read()
            encoded_content = base64.b64encode(file_content).decode('utf-8')
            
            # 通过SSH传输编码后的内容
            # 创建临时文件以避免命令过长
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp:
                temp.write(encoded_content)
                temp_path = temp.name
            
            # 读取临时文件内容分块发送
            with open(temp_path, 'r') as f:
                chunk_size = 50000  # 适当的块大小
                remote_file_escaped = remote_file.replace('"', '\\"')
                
                # 创建或清空远程文件
                clear_cmd = f'echo -n > "{remote_file_escaped}"'
                clear_result = await ssh_tool.execute(clear_cmd)
                if clear_result.error:
                    return f"准备远程文件失败 {remote_file}: {clear_result.error}"
                
                # 分块读取并追加到远程文件
                count = 0
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    
                    append_cmd = f'echo -n "{chunk}" | base64 -d >> "{remote_file_escaped}"'
                    append_result = await ssh_tool.execute(append_cmd)
                    if append_result.error:
                        return f"传输文件块失败 {remote_file} (块 {count}): {append_result.error}"
                    count += 1
            
            # 清理临时文件
            os.unlink(temp_path)
            return f"文件传输成功: {local_file} -> {remote_file}"
            
        except Exception as e:
            return f"文件传输失败 {local_file}: {str(e)}"
    