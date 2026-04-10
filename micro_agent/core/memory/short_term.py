"""短期记忆：纯内存实现，等同于旧版 Memory 类。"""

from __future__ import annotations

from typing import Any

from micro_agent.core.memory.base import MemoryProvider
from micro_agent.core.schema import Message, Role


class ShortTermMemory(MemoryProvider):
    """内存中的短期记忆。超出 max_messages 时保留 system 消息，裁剪历史。"""

    def __init__(self, max_messages: int = 200):
        self.messages: list[Message] = []
        self.max_messages = max_messages

    def add(self, message: Message) -> None:
        self.messages.append(message)
        if len(self.messages) > self.max_messages:
            system = [m for m in self.messages if m.role == Role.SYSTEM]
            other = [m for m in self.messages if m.role != Role.SYSTEM]
            keep = self.max_messages - len(system)
            self.messages = system + other[-keep:]

    def get_messages(self) -> list[Message]:
        return list(self.messages)

    def to_list(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.messages]

    def clear(self) -> None:
        self.messages.clear()

    def __len__(self) -> int:
        return len(self.messages)
