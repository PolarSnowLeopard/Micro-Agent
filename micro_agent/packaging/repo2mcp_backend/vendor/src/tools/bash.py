"""Bash 命令执行工具"""
from src.tools.base import BaseTool, ToolResult
from src.sandbox.local import LocalSandbox


class BashTool(BaseTool):
    """在本地沙箱中执行 bash 命令，对过长输出进行截断。"""

    name = "bash"
    description = "Execute a bash command. Use this to run shell commands, read/write files, install packages, or execute scripts. Output is truncated for large results."
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute"
            }
        },
        "required": ["command"]
    }

    MAX_OUTPUT_CHARS = 5000
    HEAD_RATIO = 0.3
    TAIL_RATIO = 0.6

    def __init__(self, sandbox: LocalSandbox):
        self.sandbox = sandbox

    def _truncate_output(self, output: str) -> str:
        if len(output) <= self.MAX_OUTPUT_CHARS:
            return output

        head_size = int(self.MAX_OUTPUT_CHARS * self.HEAD_RATIO)
        tail_size = int(self.MAX_OUTPUT_CHARS * self.TAIL_RATIO)
        truncated_chars = len(output) - head_size - tail_size

        head_part = output[:head_size]
        tail_part = output[-tail_size:]

        head_newline = head_part.rfind('\n')
        if head_newline > head_size * 0.8:
            head_part = head_part[:head_newline + 1]

        tail_newline = tail_part.find('\n')
        if tail_newline != -1 and tail_newline < tail_size * 0.2:
            tail_part = tail_part[tail_newline + 1:]

        truncation_msg = (
            f"\n\n... [OUTPUT TRUNCATED: {truncated_chars:,} chars omitted, "
            f"total {len(output):,} chars]\n"
            f"[TIP: Use 'head', 'tail', 'grep' for large outputs] ...\n\n"
        )
        return head_part + truncation_msg + tail_part

    def execute(self, command: str = "", **kwargs) -> ToolResult:
        if not command or not command.strip():
            return ToolResult(
                success=False, output="",
                error="Empty command. You must provide a non-empty 'command' string argument.",
            )
        result = self.sandbox.exec(command)

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr and not result.success:
            output_parts.append(f"[stderr]: {result.stderr}")

        output = "\n".join(output_parts) if output_parts else "(no output)"
        output = self._truncate_output(output)

        return ToolResult(
            success=result.success,
            output=output,
            error=result.stderr if not result.success else None
        )
