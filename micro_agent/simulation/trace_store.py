"""轨迹持久化：保存仿真执行轨迹，支持后续对比与回放。

接口设计面向未来扩展（Redis / DB），当前用 FileTraceStore 落盘。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger


@dataclass
class TraceRecord:
    """一次仿真运行的完整记录。"""

    session_id: str
    app_name: str = ""
    domain: str = ""
    mode: str = "production"
    strategy: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    success: bool = False
    iterations: int = 0
    elapsed_ms: int = 0
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TraceRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class TraceStore(ABC):
    """轨迹存储抽象接口。"""

    @abstractmethod
    async def save(self, record: TraceRecord) -> None: ...

    @abstractmethod
    async def load(self, session_id: str) -> Optional[TraceRecord]: ...

    @abstractmethod
    async def list_all(
        self, limit: int = 50, app_name: str | None = None
    ) -> list[dict]: ...

    @abstractmethod
    async def compare(self, session_ids: list[str]) -> list[dict]: ...


class FileTraceStore(TraceStore):
    """基于 JSON 文件的轨迹存储。每个 session 一个文件。"""

    def __init__(self, storage_dir: Path):
        self._dir = storage_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe_id = session_id.replace("/", "_").replace("..", "_")
        return self._dir / f"{safe_id}.json"

    async def save(self, record: TraceRecord) -> None:
        path = self._path(record.session_id)
        path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"轨迹已保存: {path} ({len(record.events)} 事件)")

    async def load(self, session_id: str) -> Optional[TraceRecord]:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return TraceRecord.from_dict(data)
        except Exception as e:
            logger.warning(f"加载轨迹失败 {session_id}: {e}")
            return None

    async def list_all(self, limit: int = 50, app_name: str | None = None) -> list[dict]:
        records = []
        files = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            if len(records) >= limit:
                break
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if app_name and data.get("app_name") != app_name:
                    continue
                records.append({
                    "recordId": data.get("session_id", f.stem),
                    "appName": data.get("app_name", ""),
                    "strategy": data.get("strategy", {}),
                    "metrics": {
                        "iterations": data.get("iterations", 0),
                        "elapsedMs": data.get("elapsed_ms", 0),
                    },
                    "createdAt": data.get("created_at", 0),
                    "success": data.get("success", False),
                })
            except Exception:
                pass
        return records

    async def compare(self, session_ids: list[str]) -> list[dict]:
        results = []
        for sid in session_ids:
            record = await self.load(sid)
            if not record:
                continue
            results.append({
                "recordId": record.session_id,
                "strategy": record.strategy,
                "metrics": {
                    "iterations": record.iterations,
                    "elapsedMs": record.elapsed_ms,
                },
                "createdAt": record.created_at,
                "success": record.success,
            })
        return results
