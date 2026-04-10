"""Memory Provider 抽象接口。

所有记忆实现（短期/文件/向量数据库）都实现此接口。
Agent 通过 MemoryProvider 操作记忆，不关心底层存储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from micro_agent.core.schema import Message


class MemoryProvider(ABC):
    """记忆存储接口。"""

    @abstractmethod
    def add(self, message: Message) -> None:
        """添加一条消息到记忆。"""

    @abstractmethod
    def get_messages(self) -> list[Message]:
        """获取所有消息。"""

    @abstractmethod
    def to_list(self) -> list[dict[str, Any]]:
        """转换为 LLM API 消息格式列表。"""

    @abstractmethod
    def clear(self) -> None:
        """清空记忆。"""

    @abstractmethod
    def __len__(self) -> int: ...

    def search(self, query: str, top_k: int = 5) -> list[Message]:
        """语义搜索相关记忆。默认返回空，向量存储实现会覆盖此方法。"""
        return []

    async def persist(self) -> None:
        """持久化到外部存储。默认无操作。"""

    async def load(self, session_id: str) -> None:
        """从外部存储加载。默认无操作。"""
