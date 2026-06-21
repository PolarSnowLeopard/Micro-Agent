"""带调用日志的 MCPTool 包装器，供仿真编排器记录真实 MCP 轨迹。"""

from __future__ import annotations

import time
import uuid as _uuid
from typing import Any

from micro_agent.simulation.sandbox_tool import ToolCallRecord
from micro_agent.tool.base import Tool, ToolResult
from micro_agent.tool.mcp.tool import MCPTool


class LoggingMCPTool(Tool):
    """包装 MCPTool：对 Planner 暴露统一 Tool 接口，并写入 call_log。"""

    def __init__(
        self,
        inner: MCPTool,
        *,
        registered_name: str,
        service_id: str,
        service_name: str,
        transport: str = "sse",
    ):
        self.name = registered_name
        self.description = inner.description
        self.parameters = inner.parameters
        self._inner = inner
        self.service_id = service_id
        self.service_name = service_name
        self._transport = transport
        self.call_log: list[ToolCallRecord] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        start = time.time()
        call_id = f"call-{_uuid.uuid4().hex[:12]}"
        result = await self._inner.execute(**kwargs)
        elapsed = int((time.time() - start) * 1000)
        self.call_log.append(
            ToolCallRecord(
                tool_name=self.name,
                service_id=self.service_id,
                arguments=dict(kwargs),
                result=result.output or "",
                error=result.error,
                latency_ms=elapsed,
                timestamp=start,
                call_id=call_id,
                service_name=self.service_name,
                channel="real_mcp",
                transport=self._transport,
                success=result.error is None,
                source="real_mcp",
            )
        )
        return result
