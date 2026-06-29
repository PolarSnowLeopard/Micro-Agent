"""仅供显式 ``isFake=true`` 服务使用的确定性假工具。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from micro_agent.simulation.trace_records import ToolCallRecord
from micro_agent.tool.base import Tool, ToolResult


@dataclass
class SandboxTool(Tool):
    """返回参数回显并记录调用；绝不充当真实 MCP 的故障回退。"""

    name: str = "sandbox_tool"
    description: str = ""
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    })
    service_id: str = ""
    service_name: str = ""
    call_log: list[ToolCallRecord] = field(default_factory=list, repr=False)

    async def execute(self, **kwargs: Any) -> ToolResult:
        started = time.time()
        call_id = f"call-{uuid.uuid4().hex[:12]}"
        if not kwargs:
            return self._record(
                started,
                kwargs,
                call_id,
                error=f"服务 [{self.service_name}] 调用缺少业务参数",
            )

        output = json.dumps({
            "success": True,
            "service": self.service_name,
            "action": kwargs.get("action") or "execute",
            "input": kwargs,
        }, ensure_ascii=False)
        return self._record(started, kwargs, call_id, output=output)

    def _record(
        self,
        started: float,
        arguments: dict,
        call_id: str,
        *,
        output: str = "",
        error: str | None = None,
    ) -> ToolResult:
        self.call_log.append(ToolCallRecord(
            tool_name=self.name,
            service_id=self.service_id,
            arguments=arguments,
            result=output,
            error=error,
            latency_ms=int((time.time() - started) * 1000),
            timestamp=started,
            call_id=call_id,
            service_name=self.service_name,
            channel="sandbox",
            transport="in_process",
            success=error is None,
            source="demo_fake_mcp",
        ))
        return ToolResult(error=error) if error else ToolResult(output=output)
