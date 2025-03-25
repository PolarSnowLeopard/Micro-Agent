from abc import ABC, abstractmethod
from typing import Optional, Tuple, Dict

from pydantic import Field

from app.agent.base import BaseAgent
from app.llm import LLM
from app.schema import AgentState, Memory, Record, TokenUsage


class ReActAgent(BaseAgent, ABC):
    name: str
    description: Optional[str] = None

    system_prompt: Optional[str] = None
    next_step_prompt: Optional[str] = None

    llm: Optional[LLM] = Field(default_factory=LLM)
    memory: Memory = Field(default_factory=Memory)
    state: AgentState = AgentState.IDLE

    max_steps: int = 10
    current_step: int = 0

    @abstractmethod
    async def think(self) -> Tuple[bool, str, str, TokenUsage]:
        """Process current state and decide next action"""

    @abstractmethod
    async def act(self) -> str:
        """Execute decided actions"""

    async def step(self) -> Dict[str, str]:
        """Execute a single step: think and act."""
        result = Record()
        should_act, thought, action, token_usage = await self.think()
        result.thought = thought
        result.action = action
        result.token_usage = token_usage
        if should_act:
            result.action_result = await self.act()
        return result.to_dict()
        # if not should_act:
        #     return "Thinking complete - no action needed"
        # return await self.act()
