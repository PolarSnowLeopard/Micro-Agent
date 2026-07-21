"""工具基类和注册器"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str
    error: Optional[str] = None

    def to_message(self) -> str:
        if self.success:
            return self.output
        else:
            return f"Error: {self.error or self.output}"


class BaseTool(ABC):
    """工具基类"""
    name: str
    description: str
    parameters: Dict

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        pass

    def to_openai_schema(self) -> Dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        self._tools[tool.name] = tool
        return self

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def execute(self, name: str, **kwargs) -> ToolResult:
        tool = self.get(name)
        if not tool:
            return ToolResult(success=False, output="", error=f"Tool not found: {name}")
        try:
            return tool.execute(**kwargs)
        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
            return ToolResult(success=False, output="", error=str(e))

    def list_schemas(self) -> List[Dict]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)
