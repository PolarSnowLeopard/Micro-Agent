"""仿真沙箱工具：在无真实 MCP 服务时提供拟真调用。

与 SimulatedMCPTool 的区别：
  - 返回格式与 MCPTool 一致（纯文本 ToolResult.output）
  - 模拟真实延迟（随机 50-300ms）
  - 可配置失败率，用于验证 Agent 的容错能力
  - 记录调用日志，供 TraceStore 持久化

后续替换为真实 MCP 时，只需在 orchestrator._build_planner() 中
把 SandboxTool 替换为 MCPTool（二者共享 Tool 接口）。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from micro_agent.tool.base import Tool, ToolResult


@dataclass
class ToolCallRecord:
    tool_name: str
    service_id: str
    arguments: dict
    result: str
    error: Optional[str]
    latency_ms: int
    timestamp: float


@dataclass
class SandboxTool(Tool):
    """拟真的 MCP 服务工具。接口与 MCPTool 对齐。"""

    name: str = "sandbox_tool"
    description: str = ""
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    })

    service_id: str = ""
    service_name: str = ""

    latency_range: tuple[int, int] = (50, 300)
    failure_rate: float = 0.0

    call_log: list[ToolCallRecord] = field(default_factory=list, repr=False)

    async def execute(self, **kwargs: Any) -> ToolResult:
        start = time.time()
        latency = random.randint(*self.latency_range)
        await asyncio.sleep(latency / 1000.0)

        if self.failure_rate > 0 and random.random() < self.failure_rate:
            error_msg = f"服务 [{self.service_name}] 调用超时或异常"
            elapsed = int((time.time() - start) * 1000)
            self.call_log.append(ToolCallRecord(
                tool_name=self.name,
                service_id=self.service_id,
                arguments=kwargs,
                result="",
                error=error_msg,
                latency_ms=elapsed,
                timestamp=start,
            ))
            return ToolResult(error=error_msg)

        output = self._generate_response(kwargs)
        elapsed = int((time.time() - start) * 1000)

        self.call_log.append(ToolCallRecord(
            tool_name=self.name,
            service_id=self.service_id,
            arguments=kwargs,
            result=output,
            error=None,
            latency_ms=elapsed,
            timestamp=start,
        ))

        return ToolResult(output=output)

    def _generate_response(self, kwargs: dict) -> str:
        """根据工具描述和参数生成拟真响应。

        格式与 MCPTool 一致：纯 JSON 文本。Agent 不应感知这是 mock。
        """
        data: dict[str, Any] = {
            "status": "success",
            "service": self.service_name,
            "data": self._infer_data(kwargs),
        }
        return json.dumps(data, ensure_ascii=False)

    def _infer_data(self, kwargs: dict) -> dict[str, Any]:
        """基于工具名称和输入推断合理的输出结构。"""
        hint = self.name.lower()

        if any(k in hint for k in ("query", "get", "list", "fetch", "search")):
            return {
                "records": [
                    {"id": f"rec-{i+1}", "summary": f"示例记录 {i+1}"}
                    for i in range(min(kwargs.get("limit", 3), 5))
                ],
                "total": kwargs.get("limit", 3),
            }

        if any(k in hint for k in ("detect", "check", "validate", "assess", "risk")):
            return {
                "passed": True,
                "score": round(random.uniform(0.7, 0.99), 3),
                "flags": [],
                "details": "检测完成，未发现异常",
            }

        if any(k in hint for k in ("create", "generate", "report", "write", "send")):
            return {
                "id": f"out-{random.randint(1000,9999)}",
                "created": True,
                "message": "操作已完成",
            }

        if any(k in hint for k in ("compute", "analy", "calculate", "aggregate")):
            return {
                "result": round(random.uniform(50, 200), 2),
                "unit": "score",
                "confidence": round(random.uniform(0.85, 0.99), 3),
            }

        if any(k in hint for k in ("transform", "convert", "format", "adapt")):
            return {
                "transformed": True,
                "input_format": kwargs.get("input_format", "raw"),
                "output_format": kwargs.get("output_format", "standard"),
            }

        return {"output": "操作完成", "input_received": list(kwargs.keys())}
