import json
from typing import Any, Optional

from app.tool.base import BaseTool, ToolResult


class FinalizeResultTool(BaseTool):
    """Agent-facing tool to submit the final result payload.

    The agent should call this tool once it believes the task is done, providing
    text_result, visualization_data, and file_result. The tool echoes back the
    provided JSON so the server can capture it as the final result.
    """

    def __init__(self) -> None:
        super().__init__(
            name="finalize_meta_result",
            description=(
                "提交最终结果。调用时请提供 text_result、visualization_data、file_result 三个字段。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text_result": {"type": ["string", "null"]},
                    "visualization_data": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                    "file_result": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                },
                "required": [],
                "additionalProperties": True,
                "description": "提交最终结果数据容器",
            },
        )

    async def execute(
        self,
        text_result: Optional[str] = None,
        visualization_data: Optional[Any] = None,
        file_result: Optional[Any] = None,
        **kwargs,
    ) -> ToolResult:
        payload = {
            "text_result": text_result,
            "visualization_data": visualization_data,
            "file_result": file_result,
        }
        try:
            return ToolResult(output=json.dumps(payload, ensure_ascii=False))
        except Exception:
            # Fallback plain text
            return ToolResult(output=str(payload))


