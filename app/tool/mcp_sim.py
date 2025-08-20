import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.tool.base import BaseTool, ToolResult


class SimulatedMCPTool(BaseTool):
    """本地模拟的MCP工具。

    在没有真实MCP服务时，根据节点与工具元信息以及调用输入，
    生成符合预期的中文模拟输出，形态上与真实调用一致。
    """

    node_id: Optional[str] = None
    node_name: Optional[str] = None
    node_des: Optional[str] = None
    tool_id: Optional[str] = None
    original_name: Optional[str] = None

    def __init__(
        self,
        name: str,
        description: str,
        node_id: Optional[str] = None,
        node_name: Optional[str] = None,
        node_des: Optional[str] = None,
        tool_id: Optional[str] = None,
        original_name: Optional[str] = None,
        parameters: Optional[dict] = None,
    ) -> None:
        # 宽松参数schema，允许自由字段，避免参数校验阻碍Agent尝试
        param_schema = parameters or {
            "type": "object",
            "description": "模拟MCP工具的输入参数，键值自由",
            "additionalProperties": True,
        }
        super().__init__(name=name, description=description, parameters=param_schema)
        self.node_id = node_id
        self.node_name = node_name
        self.node_des = node_des
        self.tool_id = tool_id
        self.original_name = original_name

    async def execute(self, **kwargs) -> ToolResult:
        # 必要信息校验
        if not self.node_name or not self.original_name:
            return ToolResult(error="模拟调用失败：缺少必要的节点或工具信息。")

        # 基于工具名生成直观的输出片段
        tool_hint = self.original_name.lower()
        if "health" in tool_hint:
            mock_payload = {"状态": "正常", "延迟毫秒": 32}
            result_desc = "服务健康状态检测完成"
        elif "report" in tool_hint:
            mock_payload = {"报告标题": f"{self.node_name} 自动生成报告", "摘要": "已基于输入数据生成分析结论与建议"}
            result_desc = "报告已生成"
        elif "compute" in tool_hint or "analy" in tool_hint:
            mock_payload = {"计算结果": "多方联合计算完成", "关键指标": {"准确率": "92.1%", "召回率": "88.3%"}}
            result_desc = "联合计算/分析完成"
        else:
            mock_payload = {"输出": "已完成模拟工具调用，返回示例结果"}
            result_desc = "工具调用完成"

        # 组织中文输出
        output: Dict[str, Any] = {
            "调用结果": result_desc,
            "服务名称": self.node_name,
            "服务说明": self.node_des or "",
            "工具名称": self.original_name,
            "工具说明": self.description or "",
            "输入参数": kwargs or {},
            "模拟数据": mock_payload,
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            return ToolResult(output=json.dumps(output, ensure_ascii=False))
        except Exception:
            # 兜底为纯文本
            text = (
                f"[{self.node_name}] 工具 {self.original_name} 已处理输入，返回模拟结果："
                f"{mock_payload}"
            )
            return ToolResult(output=text)


