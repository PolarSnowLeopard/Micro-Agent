"""核心数据模型：Message, Memory, ToolCall, AgentEvent。

所有 agent/tool/api 层共享这些类型。修改需谨慎——这是框架的数据骨干。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal, Optional


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string

    def parse_arguments(self) -> dict[str, Any]:
        try:
            return json.loads(self.arguments) if self.arguments else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class Message:
    role: Role
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: Optional[str] = None,
        tool_calls: Optional[list[ToolCall]] = None,
    ) -> Message:
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(
        cls,
        content: str,
        tool_call_id: str,
        name: Optional[str] = None,
    ) -> Message:
        return cls(
            role=Role.TOOL, content=content, tool_call_id=tool_call_id, name=name
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 LLM API 消息格式。"""
        d: dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        return d


class Memory:
    """对话记忆管理。当前为短期内存实现，Phase 5 扩展为可插拔接口。"""

    def __init__(self, max_messages: int = 200):
        self.messages: list[Message] = []
        self.max_messages = max_messages

    def add(self, message: Message) -> None:
        self.messages.append(message)
        if len(self.messages) > self.max_messages:
            system_msgs = [m for m in self.messages if m.role == Role.SYSTEM]
            other_msgs = [m for m in self.messages if m.role != Role.SYSTEM]
            keep = self.max_messages - len(system_msgs)
            self.messages = system_msgs + other_msgs[-keep:]

    def to_list(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.messages]

    def clear(self) -> None:
        self.messages.clear()

    def __len__(self) -> int:
        return len(self.messages)


EventType = Literal["think", "tool_call", "tool_result", "error", "done"]


@dataclass
class AgentEvent:
    """Agent 执行事件。前端通过 SSE 接收，按 type 分类展示。"""

    type: EventType
    step: int
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {self.to_json()}\n\n"
