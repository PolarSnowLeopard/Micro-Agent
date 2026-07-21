"""Agent 基类"""
from abc import ABC, abstractmethod
from typing import Optional

from src.llm.client import LLMClient
from src.tools.base import ToolRegistry
from src.logger import get_logger

logger = get_logger(__name__)


class BaseAgent(ABC):
    """Agent 基类，定义基本接口。"""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        system_prompt: Optional[str] = None,
    ):
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt or self._default_system_prompt()

    def _default_system_prompt(self) -> str:
        return "You are a helpful AI assistant with access to tools."

    @abstractmethod
    def run(self, task: str) -> str:
        pass
