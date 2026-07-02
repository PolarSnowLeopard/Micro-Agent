"""工具接口定义。

所有工具（本地工具 / MCP 远程工具）必须实现 Tool 抽象类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ToolResult:
    output: str = ""
    error: Optional[str] = None
    meta: Optional[dict[str, Any]] = None

    def __str__(self) -> str:
        return self.error if self.error else self.output

    def __bool__(self) -> bool:
        return bool(self.output or self.error)


class Tool(ABC):
    """统一工具接口。MCP 远程工具和本地工具都实现此接口。"""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具。子类必须实现。"""

    def to_llm_format(self) -> dict[str, Any]:
        """转换为 LLM function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
