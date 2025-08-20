from typing import Any, Dict, List, Optional, Tuple

from pydantic import Field

from app.agent.toolcall import ToolCallAgent
from app.logger import logger
from app.schema import AgentState, Message
from app.tool import ToolCollection, Terminate
from app.tool.mcp import MCPClients, MCPClientTool
from app.tool.mcp_sim import SimulatedMCPTool
from app.tool.finalize_meta_result import FinalizeResultTool


def _sanitize_identifier(text: str) -> str:
    """Create a provider-safe identifier (ASCII only, [A-Za-z0-9_-], max 128)."""
    import re
    if not text:
        return "srv"
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text))
    ident = re.sub(r"_+", "_", ident).strip("_")
    ident = ident or "srv"
    if len(ident) > 128:
        ident = ident[:128]
    return ident


class MetaAppAgent(ToolCallAgent):
    """ReAct agent that executes a meta application defined by a configuration.

    The configuration specifies a list of MCP services (SSE or stdio) and the allowed tools
    under each service. The agent connects to those services, exposes ONLY the listed tools
    (with unique, readable names), and performs ReAct-style tool calling to fulfill the user task.
    """

    name: str = "meta_app_agent"
    description: str = "An agent that runs a meta app by calling configured MCP services/tools."

    # Under the hood we still use MCP clients, but we present a filtered ToolCollection to the LLM
    mcp_clients: MCPClients = Field(default_factory=MCPClients)
    available_tools: ToolCollection = Field(default_factory=ToolCollection)

    # Track connections for cleanup
    connected_servers: List[str] = Field(default_factory=list)

    # System prompts can be customized per meta app
    system_prompt: Optional[str] = None
    next_step_prompt: Optional[str] = None

    max_steps: int = 40
    use_sim_only: bool = True

    async def initialize_from_config(self, meta_config: Dict[str, Any], use_sim_only: Optional[bool] = None) -> None:
        """Initialize connections and available tools based on the meta app configuration."""
        try:
            if use_sim_only is not None:
                self.use_sim_only = bool(use_sim_only)
            node_list: List[Dict[str, Any]] = []

            def build_nodes_from_services_all(services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                nodes: List[Dict[str, Any]] = []
                for svc in services or []:
                    apis = svc.get("apiList") or []
                    for api in apis:
                        method = (api.get("method") or "").lower()
                        if method != "sse":
                            continue
                        tools = api.get("tools") or []
                        # Normalize tool list to { id?, name, description }
                        norm_tools = []
                        for t in tools:
                            if not t:
                                continue
                            norm_tools.append({
                                "id": t.get("id"),
                                "name": t.get("name"),
                                "description": t.get("description") or t.get("des") or "",
                            })
                        nodes.append({
                            "id": svc.get("id"),
                            "name": svc.get("name"),
                            "des": api.get("des") or svc.get("name"),
                            "url": api.get("url"),
                            "method": api.get("method"),
                            "tools": norm_tools,
                        })
                return nodes

            info = meta_config.get("info")
            services_catalog = meta_config.get("services")
            if isinstance(services_catalog, list):
                node_list = build_nodes_from_services_all(services_catalog)

            if not node_list:
                raise ValueError("配置中缺少 nodeList。")

            filtered_tools: List[Any] = []
            used_names: set = set()

            if self.use_sim_only:
                # 构建模拟工具集合，不建立真实网络连接
                for idx, node in enumerate(node_list):
                    server_id = _sanitize_identifier(node.get("id") or node.get("name") or f"node_{idx}")
                    tools = node.get("tools", []) or []
                    for tool in tools:
                        original_name = tool.get("name")
                        if not original_name:
                            continue
                        alias_prefix = server_id
                        base_name = _sanitize_identifier(f"{alias_prefix}_{original_name}")
                        # enforce uniqueness
                        alias_name = base_name
                        suffix = 1
                        while alias_name in used_names:
                            candidate = f"{base_name}_{suffix}"
                            if len(candidate) > 128:
                                # trim base to fit within 128 incl. suffix
                                trim_len = 128 - len(f"_{suffix}")
                                base_short = base_name[:max(1, trim_len)]
                                candidate = f"{base_short}_{suffix}"
                            alias_name = candidate
                            suffix += 1
                        used_names.add(alias_name)
                        filtered_tools.append(
                            SimulatedMCPTool(
                                name=alias_name,
                                description=(tool.get("description") or f"[{alias_prefix}] {original_name}"),
                                node_id=node.get("id"),
                                node_name=node.get("name"),
                                node_des=node.get("des"),
                                tool_id=tool.get("id"),
                                original_name=original_name,
                                parameters={
                                    "type": "object",
                                    "description": "模拟工具输入参数",
                                    "additionalProperties": True,
                                },
                            )
                        )
                logger.info("MetaAppAgent 以模拟模式运行（use_sim_only=True），不会连接真实MCP服务。")
            else:
                # 1) Connect to each MCP server specified by nodes
                for idx, node in enumerate(node_list):
                    method = (node.get("method") or "").lower()
                    url = node.get("url")
                    if method != "sse":
                        logger.warning(f"节点 {node.get('name') or idx} 的连接方式 {method} 暂不支持，已跳过。")
                        continue

                    server_id = _sanitize_identifier(node.get("id") or node.get("name") or f"node_{idx}")
                    await self.mcp_clients.connect_sse(server_url=url, server_id=server_id)
                    self.connected_servers.append(server_id)

                # 2) Build filtered tool collection according to node.tools
                for idx, node in enumerate(node_list):
                    server_id = _sanitize_identifier(node.get("id") or node.get("name") or f"node_{idx}")
                    tools = node.get("tools", []) or []
                    for tool in tools:
                        original_name = tool.get("name")
                        if not original_name:
                            continue

                        # Find the tool schema via the MCPClients tool_map: name is f"{server_id}_{original_name}"
                        prefixed_name = f"{server_id}_{original_name}"
                        server_session = self.mcp_clients.sessions.get(server_id)
                        if not server_session:
                            logger.warning(f"服务 {server_id} 会话不存在，工具 {original_name} 跳过。")
                            continue

                        # parameters/schema: retrieve from tool_map if available
                        base_tool = self.mcp_clients.tool_map.get(prefixed_name)
                        parameters = getattr(base_tool, "parameters", {}) if base_tool else {}

                        # Create a readable and unique tool name for the agent
                        alias_prefix = server_id
                        base_name = _sanitize_identifier(f"{alias_prefix}_{original_name}")
                        alias_name = base_name
                        suffix = 1
                        while alias_name in used_names:
                            candidate = f"{base_name}_{suffix}"
                            if len(candidate) > 128:
                                trim_len = 128 - len(f"_{suffix}")
                                base_short = base_name[:max(1, trim_len)]
                                candidate = f"{base_short}_{suffix}"
                            alias_name = candidate
                            suffix += 1
                        used_names.add(alias_name)

                        filtered_tools.append(
                            MCPClientTool(
                                name=alias_name,
                                description=(tool.get("description") or f"[{alias_prefix}] {original_name}"),
                                parameters=parameters,
                                session=server_session,
                                server_id=server_id,
                                original_name=original_name,
                            )
                        )

            # Add local tools: finalize_meta_result and terminate
            terminate_tool = Terminate()
            finalize_tool = FinalizeResultTool()

            # 3) Expose only the filtered tools (plus terminate)
            self.available_tools = ToolCollection(*(filtered_tools + [finalize_tool, terminate_tool]))

            # 4) Set prompts using new field names only
            app_name = info.get("name")
            app_des = info.get("des")
            app_input_name = info.get("inputName")
            app_output_name = info.get("outputName")

            # Summarize tools for the system prompt
            tool_names = [t.name for t in self.available_tools.tools]
            tools_info = ", ".join(tool_names)

            self.system_prompt = (
                f"你是一个元应用智能体: {app_name}\n"
                f"目标: {app_des}\n"
                f"输入数据: {app_input_name}；期望输出: {app_output_name}\n\n"
                f"只能使用下列工具来完成任务（按需逐步调用）：{tools_info}\n"
                f"- 工具名称带有服务前缀，例如 service_tool；请根据任务分解一步步调用合适工具\n"
                f"- 工具调用参数需严格遵循其schema，若失败请根据报错重试\n"
                f"- 当你认为任务已完成时，请先调用 finalize_meta_result 工具提交最终结果(text_result/visualization_data/file_result)，然后再调用 terminate 结束会话"
            )
            self.next_step_prompt = (
                "基于当前进展，确定下一步要调用的工具及其参数；若已完成，请调用 terminate 结束。"
            )

            # Seed memory with system message listing available tools
            self.memory.add_message(Message.system_message(self.system_prompt))

            logger.info("MetaAppAgent 初始化完成: 已连接服务器并过滤可用工具。")
        except Exception as e:
            logger.error(f"初始化MetaAppAgent失败: {str(e)}")
            raise

    async def cleanup(self) -> None:
        """Disconnect all MCP connections created for this meta app."""
        try:
            if hasattr(self, "mcp_clients") and self.mcp_clients and self.connected_servers:
                # Copy list to avoid modification during iteration
                servers = list(self.connected_servers)
                self.connected_servers = []
                for sid in servers:
                    try:
                        await self.mcp_clients.disconnect(sid)
                    except Exception as e:
                        logger.warning(f"断开服务器 {sid} 失败: {str(e)}")
        except Exception as e:
            logger.error(f"MetaAppAgent 清理出错: {str(e)}")


