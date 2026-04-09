"""模拟 MCP 工具——在没有真实 MCP 服务时返回 mock 数据。

用于元应用预览/验证场景：前端提交元应用配置后，Agent 使用模拟工具
"走一遍流程"，验证编排逻辑是否正确，无需后端服务实际在线。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from tool.base import Tool, ToolResult


@dataclass
class SimulatedMCPTool(Tool):
    """本地模拟的 MCP 工具。根据工具名生成合理的 mock 输出。"""

    name: str = "simulated_tool"
    description: str = ""
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "additionalProperties": True,
    })
    node_id: Optional[str] = None
    node_name: Optional[str] = None
    node_des: Optional[str] = None
    tool_id: Optional[str] = None
    original_name: Optional[str] = None

    async def execute(self, **kwargs: Any) -> ToolResult:
        hint = (self.original_name or self.name).lower()

        if "health" in hint:
            payload = {"状态": "正常", "延迟毫秒": 32}
            desc = "服务健康状态检测完成"
        elif "report" in hint:
            payload = {"报告标题": f"{self.node_name} 自动生成报告", "摘要": "已基于输入数据生成分析结论与建议"}
            desc = "报告已生成"
        elif "compute" in hint or "analy" in hint:
            payload = {"计算结果": "多方联合计算完成", "关键指标": {"准确率": "92.1%", "召回率": "88.3%"}}
            desc = "联合计算/分析完成"
        else:
            payload = {"输出": "已完成模拟工具调用，返回示例结果"}
            desc = "工具调用完成"

        output = {
            "调用结果": desc,
            "服务名称": self.node_name or "",
            "服务说明": self.node_des or "",
            "工具名称": self.original_name or self.name,
            "输入参数": kwargs,
            "模拟数据": payload,
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return ToolResult(output=json.dumps(output, ensure_ascii=False))
