"""终止工具：Agent 调用此工具表示任务完成。"""

from typing import Any, Literal

from micro_agent.tool.base import Tool, ToolResult

Verdict = Literal["passed", "failed"]


def _normalize_verdict(raw: Any) -> Verdict | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ("passed", "pass"):
        return "passed"
    if value in ("failed", "fail"):
        return "failed"
    return None


class Terminate(Tool):
    name = "terminate"
    description = (
        "终止当前任务并返回最终结果。"
        "验证场景下必须同时填写 verdict（passed/failed）与 result（审查摘要）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "result": {
                "type": "string",
                "description": "任务的最终结果或总结。",
            },
            "verdict": {
                "type": "string",
                "enum": ["passed", "failed"],
                "description": "验证场景必填：passed=通过，failed=未通过。",
            },
        },
        "required": ["result"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        output = kwargs.get("result", "任务已终止。")
        verdict = _normalize_verdict(kwargs.get("verdict"))
        meta = {"verdict": verdict} if verdict else None
        return ToolResult(output=output, meta=meta)
