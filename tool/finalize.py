"""FinalizeResult 工具——Agent 用来提交元应用最终结果。

元应用执行完毕后，Agent 调用此工具汇总 text_result / visualization_data / file_result，
上层 API 可从事件流中捕获此工具的输出作为最终结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from tool.base import Tool, ToolResult


@dataclass
class FinalizeResult(Tool):
    name: str = "finalize_meta_result"
    description: str = "提交最终结果。请提供 text_result、visualization_data、file_result 三个字段。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "text_result": {"type": ["string", "null"]},
            "visualization_data": {},
            "file_result": {},
        },
        "additionalProperties": True,
    })

    async def execute(
        self,
        text_result: Optional[str] = None,
        visualization_data: Any = None,
        file_result: Any = None,
        **kwargs: Any,
    ) -> ToolResult:
        payload = {
            "text_result": text_result,
            "visualization_data": visualization_data,
            "file_result": file_result,
        }
        return ToolResult(output=json.dumps(payload, ensure_ascii=False))
