"""MetaAppAgent：基于配置编排多个 MCP 服务的元应用 Agent。

与旧版的区别：
- 不继承 ToolCallAgent，而是继承 MCPAgent（只多了 connect 能力）
- 不重写 run() 循环——复用 Agent.run() 的 async generator
- 支持两种模式：sim（模拟工具）和 real（真实 MCP 连接）
- 清理由 async with 保证，无 fire-and-forget

用法：
    agent = MetaAppAgent(llm=llm)
    await agent.initialize_from_config(config, use_sim=True)
    async with agent:
        async for event in agent.run("请执行元应用"):
            ...
"""

from __future__ import annotations

import re
from typing import Any, Optional

from loguru import logger

from micro_agent.core.llm import LLM
from micro_agent.core.mcp_agent import MCPAgent
from micro_agent.tool.finalize import FinalizeResult
from micro_agent.tool.mcp.connection import ServerConfig
from micro_agent.tool.registry import ToolRegistry
from micro_agent.tool.simulated_mcp import SimulatedMCPTool
from micro_agent.tool.terminate import Terminate


def _sanitize_id(text: str, max_len: int = 128) -> str:
    """ASCII-safe identifier: [A-Za-z0-9_-], max_len chars."""
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "srv"))
    ident = re.sub(r"_+", "_", ident).strip("_") or "srv"
    return ident[:max_len]


class MetaAppAgent(MCPAgent):
    """元应用 Agent：根据前端传来的元应用配置初始化工具集。"""

    def __init__(
        self,
        *,
        llm: LLM,
        name: str = "meta_app_agent",
        max_steps: int = 40,
        **kwargs: Any,
    ):
        tools = ToolRegistry()
        tools.register(Terminate())
        tools.register(FinalizeResult())
        super().__init__(
            name=name, llm=llm, tools=tools, max_steps=max_steps, **kwargs
        )

    async def initialize_from_config(
        self,
        meta_config: dict[str, Any],
        use_sim: bool = True,
    ) -> None:
        """根据元应用配置初始化工具和 prompt。

        meta_config 格式（由前端传入）：
        {
            "info": { "name", "des", "inputName", "outputName", "outputVisualization" },
            "services": [
                {
                    "id", "name",
                    "apiList": [
                        { "url", "method", "des", "tools": [{ "id", "name", "description" }] }
                    ]
                }
            ]
        }
        """
        node_list = self._extract_nodes(meta_config)
        if not node_list:
            raise ValueError("配置中未找到有效的 MCP 服务节点")

        if use_sim:
            self._register_sim_tools(node_list)
            logger.info("MetaAppAgent 以模拟模式运行")
        else:
            await self._connect_real_services(node_list)
            logger.info("MetaAppAgent 已连接真实 MCP 服务")

        self._set_prompts(meta_config, use_sim)

    # ------ 内部方法 ------

    @staticmethod
    def _extract_nodes(config: dict) -> list[dict]:
        """从 services 列表提取 SSE 节点 + 工具信息。"""
        nodes = []
        for svc in config.get("services") or []:
            for api in svc.get("apiList") or []:
                if (api.get("method") or "").lower() != "sse":
                    continue
                raw_tools = api.get("tools") or []
                tools = [
                    {"id": t.get("id"), "name": t.get("name"),
                     "description": t.get("description") or t.get("des") or ""}
                    for t in raw_tools if t and t.get("name")
                ]
                nodes.append({
                    "id": svc.get("id"),
                    "name": svc.get("name"),
                    "des": api.get("des") or svc.get("name"),
                    "url": api.get("url"),
                    "tools": tools,
                })
        return nodes

    def _register_sim_tools(self, nodes: list[dict]) -> None:
        used: set[str] = set()
        for node in nodes:
            prefix = _sanitize_id(node.get("id") or node.get("name") or "srv")
            for t in node.get("tools", []):
                alias = self._unique_name(prefix, t["name"], used)
                self.tools.register(SimulatedMCPTool(
                    name=alias,
                    description=t.get("description") or f"[{prefix}] {t['name']}",
                    node_id=node.get("id"),
                    node_name=node.get("name"),
                    node_des=node.get("des"),
                    tool_id=t.get("id"),
                    original_name=t["name"],
                ))

    async def _connect_real_services(self, nodes: list[dict]) -> None:
        for node in nodes:
            url = node.get("url")
            if not url:
                continue
            server_id = _sanitize_id(node.get("id") or node.get("name"))
            try:
                await self.connect(ServerConfig(
                    connection_type="sse",
                    server_url=url,
                    server_id=server_id,
                ))
            except Exception as e:
                logger.warning(f"连接 {server_id} ({url}) 失败: {e}")

    def _set_prompts(self, config: dict, use_sim: bool) -> None:
        info = config.get("info") or {}
        app_name = info.get("name", "元应用")
        app_des = info.get("des", "")
        input_name = info.get("inputName", "输入数据")
        output_name = info.get("outputName", "输出结果")

        tool_names = self.tools.list_names()

        mode_hint = "（当前为模拟模式，工具返回 mock 数据）" if use_sim else ""
        self.system_prompt = (
            f"你是元应用智能体: {app_name}\n"
            f"目标: {app_des}\n"
            f"输入: {input_name}  期望输出: {output_name}\n"
            f"{mode_hint}\n\n"
            f"可用工具: {', '.join(tool_names)}\n"
            f"- 按需逐步调用合适工具完成任务\n"
            f"- 完成后先调用 finalize_meta_result 提交结果，再调用 terminate 结束"
        )
        self.next_step_prompt = "基于当前进展，确定下一步要调用的工具及参数；若已完成请调用 terminate。"

    @staticmethod
    def _unique_name(prefix: str, name: str, used: set[str]) -> str:
        base = _sanitize_id(f"{prefix}_{name}")
        alias = base
        suffix = 1
        while alias in used:
            alias = f"{base}_{suffix}"[:128]
            suffix += 1
        used.add(alias)
        return alias
