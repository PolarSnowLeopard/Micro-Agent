import asyncio
import os
import sys
import uuid
from typing import Optional

from app.exceptions import ToolError
from app.tool.base import BaseTool, CLIResult


_CMD_DESCRIPTION = """在Windows终端中执行cmd命令。
* 长时间运行的命令：对于可能无限期运行的命令，应该在后台运行并将输出重定向到文件，例如 command = `start /b python app.py > server.log 2>&1`。
* 交互式：如果cmd命令返回退出码 `-1`，这意味着进程尚未完成。助手必须向终端发送第二个调用，其中包含空的 `command`（这将检索任何额外的日志），或者可以发送额外的文本（将 `command` 设置为文本）到正在运行的进程的STDIN，或者可以发送 command=`Ctrl+C` 来中断进程。
* 超时：如果命令执行结果显示"命令超时。向进程发送中断信号"，助手应该尝试在后台重新运行该命令。
"""


class _CmdSession:
    """Windows cmd shell的会话。"""

    _started: bool
    _process: asyncio.subprocess.Process

    command: str = "cmd.exe"  # 使用默认参数，避免静默模式导致问题
    _output_delay: float = 0.1  # 减少延迟，更快地检测输出变化
    _timeout: float = 30.0  # 减少默认超时时间
    _sentinel: str = None  # 动态生成的哨兵

    def __init__(self):
        self._started = False
        self._timed_out = False
        # 创建唯一的哨兵标记，避免与输出冲突
        self._sentinel = f"CMD_EXIT_MARK_{uuid.uuid4().hex[:8]}"

    async def start(self):
        if self._started:
            return

        # 在Windows中，避免使用shell=True可能会更稳定
        self._process = await asyncio.create_subprocess_exec(
            "cmd.exe",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._started = True
        
        # 初始化命令会话，关闭回显并设置代码页
        if self._process.stdin:
            # 设置代码页为UTF-8，提高中文支持
            await self._send_command("chcp 65001")
            # 关闭命令回显
            await self._send_command("@echo off")
            # 清空任何初始化消息
            await self._read_output(clear=True)

    async def _send_command(self, cmd):
        """发送单个命令到CMD进程"""
        if self._process.stdin:
            self._process.stdin.write(f"{cmd}\r\n".encode('utf-8'))
            await self._process.stdin.drain()

    async def _read_output(self, timeout=1.0, clear=False):
        """读取当前可用的输出"""
        output = ""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    await asyncio.sleep(0.05)
                    if self._process.stdout:
                        chunk = self._process.stdout._buffer.decode('utf-8', errors='replace')
                        if chunk:
                            output += chunk
                            self._process.stdout._buffer.clear()
                        else:
                            # 如果没有更多数据，等待一点时间
                            await asyncio.sleep(0.1)
                            # 再次检查
                            new_chunk = self._process.stdout._buffer.decode('utf-8', errors='replace')
                            if not new_chunk:
                                break
                            output += new_chunk
                            self._process.stdout._buffer.clear()
        except asyncio.TimeoutError:
            # 超时不是问题，只是意味着没有更多输出
            pass
        
        # 如果只是想清空缓冲区，直接返回空
        if clear:
            return ""
        return output

    def stop(self):
        """终止cmd shell。"""
        if not self._started:
            raise ToolError("会话尚未启动。")
        if self._process.returncode is not None:
            return
        self._process.terminate()

    async def run(self, command: str):
        """在cmd shell中执行命令。"""
        if not self._started:
            raise ToolError("会话尚未启动。")
        if self._process.returncode is not None:
            return CLIResult(
                system="工具必须重新启动",
                error=f"cmd已退出，返回码为 {self._process.returncode}",
            )
        if self._timed_out:
            raise ToolError(
                f"超时：cmd在 {self._timeout} 秒内未返回，必须重新启动",
            )

        # 我们知道这些不是None，因为我们使用PIPEs创建了进程
        assert self._process.stdin
        assert self._process.stdout
        assert self._process.stderr
        
        # 清除现有缓冲区
        await self._read_output(clear=True)
        
        # 单独执行命令，然后添加哨兵以检测完成
        await self._send_command(command)
        # 首先等待命令执行完成（等待输出稳定）
        initial_output = await self._read_output()
        
        # 发送哨兵命令
        await self._send_command(f"echo {self._sentinel}")
        
        # 读取完整输出直到哨兵出现
        try:
            output = initial_output
            async with asyncio.timeout(self._timeout):
                while True:
                    chunk = await self._read_output(timeout=1.0)
                    output += chunk
                    
                    if self._sentinel in output:
                        # 找到哨兵，提取命令输出部分
                        output = output[:output.index(self._sentinel)]
                        break
                    
                    # 如果没有新数据且已经有一些输出，可能命令已完成
                    if not chunk and output:
                        # 再次尝试检查哨兵
                        await asyncio.sleep(0.5)
                        final_check = await self._read_output(timeout=0.5)
                        output += final_check
                        if self._sentinel in output:
                            output = output[:output.index(self._sentinel)]
                        break
        except asyncio.TimeoutError:
            self._timed_out = True
            raise ToolError(
                f"超时：cmd在 {self._timeout} 秒内未返回，必须重新启动",
            ) from None

        # 读取错误流
        error = ""
        if self._process.stderr and self._process.stderr._buffer:
            error = self._process.stderr._buffer.decode('utf-8', errors='replace')
            self._process.stderr._buffer.clear()

        # 清理输出
        output = output.strip()
        error = error.strip()

        return CLIResult(output=output, error=error)


class Cmd(BaseTool):
    """用于执行Windows cmd命令的工具"""

    name: str = "cmd"
    description: str = _CMD_DESCRIPTION
    parameters: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的cmd命令。当前一个退出码为 `-1` 时，可以为空以查看额外的日志。可以是 `Ctrl+C` 来中断当前运行的进程。",
            },
        },
        "required": ["command"],
    }

    _session: Optional[_CmdSession] = None

    async def execute(
        self, command: str | None = None, restart: bool = False, **kwargs
    ) -> CLIResult:
        if restart:
            if self._session:
                self._session.stop()
            self._session = _CmdSession()
            await self._session.start()
            return CLIResult(system="工具已重新启动。")

        if self._session is None:
            self._session = _CmdSession()
            await self._session.start()

        if command is not None:
            # 添加超时保护，确保即使底层出现问题也能返回
            try:
                return await asyncio.wait_for(
                    self._session.run(command),
                    timeout=30.0  # 强制最大超时时间
                )
            except asyncio.TimeoutError:
                # 如果超时，返回部分结果并重启会话
                result = CLIResult(
                    output=f"命令执行已超时，但可能已部分完成：{command}",
                    error="执行超时，会话将重新启动",
                    system="工具遇到超时问题，已自动重启"
                )
                # 后台异步重启会话
                asyncio.create_task(self._restart_session())
                return result

        raise ToolError("未提供命令。")
    
    async def _restart_session(self):
        """后台重启会话，避免阻塞主流程"""
        if self._session:
            self._session.stop()
        self._session = _CmdSession()
        await self._session.start()


if __name__ == "__main__":
    cmd = Cmd()
    if len(sys.argv) > 1:
        command = sys.argv[1]
    else:
        command = "dir"
    rst = asyncio.run(cmd.execute(command))
    print(rst) 