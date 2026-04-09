"""工具注册中心。

统一管理所有工具的注册、查找、执行。Agent 通过 ToolRegistry 与工具交互。
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from tool.base import Tool, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, namespace: Optional[str] = None) -> str:
        """注册工具，返回注册后的名称。namespace 用于 MCP 等场景的名称隔离。"""
        name = f"{namespace}_{tool.name}" if namespace else tool.name
        if name in self._tools:
            logger.warning(f"工具 '{name}' 已存在，将被覆盖")
        self._tools[name] = tool
        return name

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def unregister_by_namespace(self, namespace: str) -> list[str]:
        """移除指定 namespace 下的所有工具。"""
        prefix = f"{namespace}_"
        to_remove = [n for n in self._tools if n.startswith(prefix)]
        for n in to_remove:
            del self._tools[n]
        return to_remove

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    async def execute(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(error=f"未知工具: {name}")
        try:
            return await tool.execute(**kwargs)
        except Exception as e:
            logger.error(f"工具 '{name}' 执行失败: {e}")
            return ToolResult(error=str(e))

    def to_llm_format(self) -> list[dict[str, Any]]:
        """所有已注册工具转换为 LLM function calling 格式。"""
        result = []
        for name, tool in self._tools.items():
            fmt = tool.to_llm_format()
            fmt["function"]["name"] = name
            result.append(fmt)
        return result

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
