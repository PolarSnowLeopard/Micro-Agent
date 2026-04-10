"""Bash 工具：在持久 bash 会话中执行命令。

从旧版 Micro-Agent/app/tool/bash.py 移植，简化了 Pydantic 依赖。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from loguru import logger

from micro_agent.tool.base import Tool, ToolResult

_DESCRIPTION = """在终端中执行 bash 命令。
* 长时间命令应在后台运行并重定向输出，如: command = `python3 app.py > server.log 2>&1 &`
* 命令超时后会话会自动重启，需重新执行命令。
"""


class _BashSession:
    """持久化的 bash 会话。通过 sentinel 标记检测命令完成。"""

    _SENTINEL = "<<exit>>"

    def __init__(self, timeout: float = 120.0):
        self._process: Optional[asyncio.subprocess.Process] = None
        self._timeout = timeout
        self._timed_out = False

    async def start(self) -> None:
        if self._process and self._process.returncode is None:
            return
        self._process = await asyncio.create_subprocess_shell(
            "/bin/bash",
            preexec_fn=os.setsid,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._timed_out = False

    async def run(self, command: str) -> ToolResult:
        if not self._process or self._process.returncode is not None:
            return ToolResult(error="bash 会话未启动或已退出，请重试")

        if self._timed_out:
            await self._restart()
            return ToolResult(
                error="上一次命令超时，会话已自动重启，请重新执行命令"
            )

        assert self._process.stdin and self._process.stdout and self._process.stderr

        # 发送前清空 buffer，防止上一条命令残留
        self._process.stdout._buffer.clear()  # type: ignore[attr-defined]
        self._process.stderr._buffer.clear()  # type: ignore[attr-defined]

        self._process.stdin.write(
            f"{command}\necho '{self._SENTINEL}'\n".encode()
        )
        await self._process.stdin.drain()

        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    await asyncio.sleep(0.2)
                    output = self._process.stdout._buffer.decode()  # type: ignore[attr-defined]
                    if self._SENTINEL in output:
                        output = output[: output.index(self._SENTINEL)]
                        break
        except asyncio.TimeoutError:
            self._timed_out = True
            return ToolResult(error=f"命令超时（{self._timeout}s），会话将自动重启")

        error = self._process.stderr._buffer.decode()  # type: ignore[attr-defined]

        self._process.stdout._buffer.clear()  # type: ignore[attr-defined]
        self._process.stderr._buffer.clear()  # type: ignore[attr-defined]

        output = output.rstrip("\n")
        error = error.rstrip("\n")

        if error and not output:
            return ToolResult(error=error)
        if error:
            return ToolResult(output=f"{output}\n[stderr]: {error}")
        return ToolResult(output=output)

    async def _restart(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
        self._process = None
        await self.start()

    def stop(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()


class Bash(Tool):
    name = "bash"
    description = _DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 bash 命令。",
            },
        },
        "required": ["command"],
    }

    def __init__(self, timeout: float = 120.0):
        self._session = _BashSession(timeout=timeout)

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command")
        if not command:
            return ToolResult(error="未提供命令")
        await self._session.start()
        return await self._session.run(command)
