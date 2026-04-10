"""终止工具：Agent 调用此工具表示任务完成。"""

from typing import Any

from micro_agent.tool.base import Tool, ToolResult


class Terminate(Tool):
    name = "terminate"
    description = "终止当前任务并返回最终结果。当你认为任务已完成时调用此工具。"
    parameters = {
        "type": "object",
        "properties": {
            "result": {
                "type": "string",
                "description": "任务的最终结果或总结。",
            }
        },
        "required": ["result"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(output=kwargs.get("result", "任务已终止。"))
