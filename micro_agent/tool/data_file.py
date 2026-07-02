"""Thin ToolRegistry adapters for run-scoped data files."""

from __future__ import annotations

import time
import uuid
from typing import Any

from micro_agent.data_file import (
    DEFAULT_READ_ROWS,
    MAX_READ_ROWS,
    DataFileError,
    FileRecord,
    FileRegistry,
    bounded_result,
    dump_result,
    inspect_data_file,
    read_data_file,
)
from micro_agent.simulation.trace_records import ToolCallRecord
from micro_agent.tool.base import Tool, ToolResult


class _DataFileTool(Tool):
    def __init__(self, registry: FileRegistry) -> None:
        self.registry, self.call_log = registry, []

    def finish(self, started: float, args: dict[str, Any], record: FileRecord | None, payload: dict[str, Any]) -> ToolResult:
        payload = bounded_result(payload)
        output, error = dump_result(payload), payload.get("error")
        summary = {
            "file_id": args.get("file_id"), "file_hash": record.sha256 if record else None,
            "format": record.format if record else None, "sheet": args.get("sheet"),
            "offset": args.get("offset"), "limit": args.get("limit"), "columns": args.get("columns"),
            "returned_rows": payload.get("returned_rows", 0), "returned_characters": len(output),
            "truncated": bool(payload.get("truncated")),
        }
        self.call_log.append(ToolCallRecord(
            tool_name=self.name, service_id="local_data_file", arguments=args,
            result=dump_result(summary), error=dump_result(error) if error else None,
            latency_ms=int((time.time() - started) * 1000), timestamp=started,
            call_id=f"call-{uuid.uuid4().hex[:12]}", service_name="DataFileTool",
            channel="local_tool", transport="in_process", success=not error, source="local_data_file",
        ))
        return ToolResult(error=output) if error else ToolResult(output=output)


class InspectDataFile(_DataFileTool):
    name = "inspect_data_file"
    description = "检查当前运行已注册数据文件的结构和少量样例。只接受 file_id，不接受路径。"
    parameters = {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}

    async def execute(self, **kwargs: Any) -> ToolResult:
        started, record = time.time(), None
        args = {"file_id": kwargs.get("file_id")}
        try:
            record = self.registry.get(args["file_id"], self.registry.run_id)
            payload = inspect_data_file(record)
        except (DataFileError, OSError) as exc:
            payload = exc.payload() if isinstance(exc, DataFileError) else DataFileError("FILE_READ_FAILED", "文件读取失败。").payload()
        return self.finish(started, args, record, payload)


class ReadDataFile(_DataFileTool):
    name = "read_data_file"
    description = "按 sheet、行范围和列名读取当前运行的数据文件；单次最多返回有限行。只接受 file_id。"
    parameters = {
        "type": "object",
        "properties": {
            "file_id": {"type": "string"}, "sheet": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_ROWS},
            "columns": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["file_id"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        started, record = time.time(), None
        args = {k: kwargs[k] for k in ("file_id", "sheet", "offset", "limit", "columns") if kwargs.get(k) is not None}
        try:
            offset, requested = int(kwargs.get("offset", 0)), int(kwargs.get("limit", DEFAULT_READ_ROWS))
            if offset < 0 or requested < 1:
                raise DataFileError("INVALID_RANGE", "offset 必须大于等于 0，limit 必须大于 0。")
            columns = kwargs.get("columns")
            if columns is not None and (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)):
                raise DataFileError("INVALID_COLUMNS", "columns 必须是列名数组。")
            args.update({"offset": offset, "limit": requested})
            record = self.registry.get(args["file_id"], self.registry.run_id)
            payload = read_data_file(record, kwargs.get("sheet"), offset, min(requested, MAX_READ_ROWS), columns)
            payload["requested_limit"] = requested
            if requested > MAX_READ_ROWS:
                payload["warnings"].append(f"limit 已限制为 {MAX_READ_ROWS}。")
        except (DataFileError, TypeError, ValueError, OSError) as exc:
            if isinstance(exc, DataFileError):
                payload = exc.payload()
            elif isinstance(exc, OSError):
                payload = DataFileError("FILE_READ_FAILED", "文件读取失败。").payload()
            else:
                payload = DataFileError("INVALID_RANGE", "offset 或 limit 非法。").payload()
        return self.finish(started, args, record, payload)


def data_file_tools(registry: FileRegistry) -> list[Tool]:
    return [InspectDataFile(registry), ReadDataFile(registry)]


def data_file_context(registry: FileRegistry, file_ids: list[str]) -> str:
    rows = registry.metadata(file_ids)
    lines = [f"当前任务附带 {len(rows)} 个数据文件："]
    lines += [f"- file_id: {r['file_id']}；文件名: {r['original_name']}；类型: {r['format'].upper()}；大小: {r['size_bytes']} bytes" for r in rows]
    return "\n".join(lines) + "\n请先调用 inspect_data_file 查看结构，再按需调用 read_data_file；不要猜测文件内容。"


__all__ = ["InspectDataFile", "ReadDataFile", "data_file_tools", "data_file_context"]
