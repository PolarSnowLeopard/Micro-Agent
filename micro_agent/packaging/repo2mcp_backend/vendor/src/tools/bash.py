"""Bash 命令执行工具"""
import re
import shlex

from src.tools.base import BaseTool, ToolResult
from src.sandbox.local import LocalSandbox


class BashTool(BaseTool):
    """在本地沙箱中执行 bash 命令，对过长输出进行截断。"""

    name = "bash"
    description = (
        "Execute a bash command for repository inspection, file generation, or "
        "diagnostic scripts. Host package installation is forbidden; declare "
        "runtime packages in requirements.txt and validate them in Docker. "
        "Output is truncated for large results."
    )
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
    SHELL_CONTROL_TOKENS = {";", "&&", "||", "|", "&"}

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

    @classmethod
    def _contains_host_package_install(cls, command: str) -> bool:
        """Detect executed package-manager commands, not quoted Dockerfile text."""
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            # Let bash report malformed quoting rather than guessing at content.
            return False

        starts = [0]
        starts.extend(
            index + 1
            for index, token in enumerate(tokens)
            if token in cls.SHELL_CONTROL_TOKENS and index + 1 < len(tokens)
        )
        assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
        python = re.compile(r"^python(?:3)?(?:\.\d+)?$")
        pip = re.compile(r"^pip(?:3(?:\.\d+)?)?$")

        for start in starts:
            segment = tokens[start:]
            while segment and (
                assignment.match(segment[0])
                or segment[0] in {"command", "env", "nohup", "sudo"}
                or segment[0].startswith("-")
            ):
                segment = segment[1:]
            if len(segment) >= 4 and python.match(segment[0]):
                if segment[1:4] == ["-m", "pip", "install"]:
                    return True
            if len(segment) < 2:
                continue
            executable, action = segment[0].lower(), segment[1].lower()
            if pip.match(executable) and action == "install":
                return True
            if executable == "uv" and (
                action in {"add", "sync"}
                or (action == "pip" and len(segment) >= 3 and segment[2] == "install")
            ):
                return True
            if executable in {"conda", "mamba", "micromamba"} and action in {
                "install",
                "create",
                "update",
            }:
                return True
            if executable in {"apt", "apt-get"} and action in {
                "install",
                "update",
                "upgrade",
            }:
                return True
            if executable == "apk" and action == "add":
                return True
            if executable in {"dnf", "yum"} and action == "install":
                return True
            if executable == "poetry" and action == "add":
                return True
        return False

    def execute(self, command: str = "", **kwargs) -> ToolResult:
        if not command or not command.strip():
            return ToolResult(
                success=False, output="",
                error="Empty command. You must provide a non-empty 'command' string argument.",
            )
        if self._contains_host_package_install(command):
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Host package installation is forbidden. Add dependencies to "
                    "the generated requirements files and let Docker validate them."
                ),
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
