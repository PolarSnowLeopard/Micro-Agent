"""文件持久化记忆：JSON 文件存储，支持跨会话保留上下文。

适用场景：单用户/低频访问的持久化需求。
高并发场景建议用 Redis 或数据库实现 MemoryProvider。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from micro_agent.core.memory.base import MemoryProvider
from micro_agent.core.memory.short_term import ShortTermMemory
from micro_agent.core.schema import Message, Role, ToolCall


class FileMemory(MemoryProvider):
    """基于 JSON 文件的持久化记忆。

    写操作先更新内存，persist() 时刷盘。
    """

    def __init__(self, storage_dir: Path, max_messages: int = 200):
        self._dir = storage_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._inner = ShortTermMemory(max_messages)
        self._session_id: Optional[str] = None

    def add(self, message: Message) -> None:
        self._inner.add(message)

    def get_messages(self) -> list[Message]:
        return self._inner.get_messages()

    def to_list(self) -> list[dict[str, Any]]:
        return self._inner.to_list()

    def clear(self) -> None:
        self._inner.clear()

    def __len__(self) -> int:
        return len(self._inner)

    async def persist(self) -> None:
        if not self._session_id:
            return
        path = self._dir / f"{self._session_id}.json"
        data = [m.to_dict() for m in self._inner.messages]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug(f"记忆已持久化: {path} ({len(data)} 条)")

    async def load(self, session_id: str) -> None:
        self._session_id = session_id
        path = self._dir / f"{session_id}.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw:
                tool_calls = None
                if "tool_calls" in item:
                    tool_calls = [
                        ToolCall(
                            id=tc["id"],
                            name=tc["function"]["name"],
                            arguments=tc["function"].get("arguments", "{}"),
                        )
                        for tc in item["tool_calls"]
                    ]
                msg = Message(
                    role=Role(item["role"]),
                    content=item.get("content"),
                    tool_calls=tool_calls,
                    tool_call_id=item.get("tool_call_id"),
                    name=item.get("name"),
                )
                self._inner.add(msg)
            logger.debug(f"记忆已加载: {path} ({len(raw)} 条)")
        except Exception as e:
            logger.warning(f"加载记忆失败: {e}")
