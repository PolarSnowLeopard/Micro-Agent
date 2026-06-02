"""仿真沙箱工具：在无真实 MCP 服务时提供拟真调用。

设计目标：对 Agent（调用者）而言表现应与真实 MCP 一致：
  - 返回格式与 MCPTool 一致（纯文本 ToolResult.output / .error）
  - 模拟真实延迟（随机 100-500ms）
  - 参数驱动：缺必要参数 / 类型错误 → 稳定返回业务错误（非随机）
  - 可配置随机失败率，用于验证 Agent 的容错与重试能力
  - 记录调用日志，供 TraceStore 持久化

后续替换为真实 MCP 时，只需在 orchestrator._register_tools() 中
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

    latency_range: tuple[int, int] = (100, 500)
    failure_rate: float = 0.0

    call_log: list[ToolCallRecord] = field(default_factory=list, repr=False)
    _call_count: int = field(default=0, repr=False)

    async def execute(self, **kwargs: Any) -> ToolResult:
        start = time.time()
        self._call_count += 1
        latency = random.randint(*self.latency_range)
        await asyncio.sleep(latency / 1000.0)

        # 1) 参数校验：缺 action → 稳定报错（对 Agent 可学习）
        action = kwargs.get("action", "")
        if not action and not kwargs:
            return self._record_error(start, kwargs, f"服务 [{self.service_name}] 调用缺少参数，请提供 action 或业务参数")

        # 2) 随机失败（模拟真实 MCP 的偶发超时 / 异常）
        if self.failure_rate > 0 and random.random() < self.failure_rate:
            return self._record_error(start, kwargs, f"服务 [{self.service_name}] 调用超时或异常（可重试）")

        # 3) 正常响应
        output = self._generate_response(kwargs, action)
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

    def _record_error(self, start: float, kwargs: dict, error_msg: str) -> ToolResult:
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

    def _generate_response(self, kwargs: dict, action: str) -> str:
        """根据 action / 工具描述 / 参数生成拟真响应。

        格式与 MCPTool 一致：纯 JSON 文本。Agent 不应感知这是 mock。
        响应内容由调用参数决定，同样的输入产生结构一致的输出。
        """
        # health_check 单独处理
        if action == "health_check":
            return json.dumps({
                "status": "healthy",
                "service": self.service_name,
                "latency_ms": random.randint(*self.latency_range),
            }, ensure_ascii=False)

        data: dict[str, Any] = {
            "status": "success",
            "service": self.service_name,
            "action": action or "execute",
            "data": self._infer_data(kwargs, action),
        }

        # 回显 Agent 传入的关键参数，便于 Verifier 审查数据流转
        input_keys = [k for k in kwargs if k != "action"]
        if input_keys:
            data["input_echo"] = {k: _summarize_value(kwargs[k]) for k in input_keys[:5]}

        return json.dumps(data, ensure_ascii=False)

    def _infer_data(self, kwargs: dict, action: str) -> dict[str, Any]:
        """基于 action、工具名称和输入推断合理的输出结构。"""
        hint = (action + " " + self.name).lower()

        if any(k in hint for k in ("query", "get", "list", "fetch", "search")):
            limit = min(int(kwargs.get("limit", 3)), 10)
            return {
                "records": [
                    {"id": f"rec-{i+1}", "summary": f"示例记录 {i+1}"}
                    for i in range(limit)
                ],
                "total": limit,
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


def _summarize_value(v: Any, max_len: int = 80) -> Any:
    """截断过长的值用于 input_echo。"""
    if isinstance(v, str) and len(v) > max_len:
        return v[:max_len] + "…"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
        if len(s) > max_len:
            return s[:max_len] + "…"
        return v
    return v
