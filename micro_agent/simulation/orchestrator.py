"""仿真编排器：4 阶段流程 + Planner/Verifier 双 Agent 核心循环。

工具层（双通道，与平台目录一致）：
  - isFake=false 且已登记 SSE 地址（mcpUrl）→ 真实 MCPTool（LoggingMCPTool 记录轨迹）
  - isFake=true 或未登记可连 SSE → SandboxTool（进程内拟真）
  - 真实 MCP 连接失败时回退 SandboxTool
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from loguru import logger

from micro_agent.core.agent import Agent
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.core.schema import AgentEvent
from micro_agent.core.skill import SkillRegistry
from micro_agent.simulation.logging_mcp_tool import LoggingMCPTool
from micro_agent.simulation.sandbox_tool import SandboxTool, ToolCallRecord
from micro_agent.tool.mcp.connection import MCPConnectionManager, ServerConfig
from micro_agent.tool.registry import ToolRegistry
from micro_agent.tool.terminate import Terminate


# ---------------------------------------------------------------------------
# SimulationEvent：与前端约定的 SSE 事件
# ---------------------------------------------------------------------------

@dataclass
class SimulationEvent:
    type: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "timestamp": self.timestamp}


@dataclass
class _McpServiceBinding:
    service_id: str
    service_name: str
    server_id: str
    mcp_url: str
    tools: list[LoggingMCPTool]


def _sanitize(text: str, max_len: int = 128) -> str:
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "srv"))
    return re.sub(r"_+", "_", ident).strip("_")[:max_len] or "srv"


def _resolve_mcp_url(svc: dict[str, Any]) -> str:
    """从 servicesMeta 项解析 MCP SSE 地址（兼容 mcpUrl / url）。"""
    return str(svc.get("mcpUrl") or svc.get("url") or "").strip()


def _is_sse_mcp_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _is_catalog_fake(svc: dict[str, Any]) -> bool:
    """平台目录 isFake：true 表示演示/剧本，仿真不走真实 MCP。"""
    v = svc.get("isFake", svc.get("is_fake"))
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes")


def _mcp_transport_method(svc: dict[str, Any]) -> str:
    return str(svc.get("mcpMethod") or svc.get("method") or "sse").lower()


def _should_use_real_mcp(svc: dict[str, Any]) -> bool:
    """是否尝试真实 MCP：以目录 isFake 为准，并需可连的传输配置。"""
    if _is_catalog_fake(svc):
        return False
    method = _mcp_transport_method(svc)
    if method == "stdio":
        return bool(str(svc.get("mcpCommand") or "").strip())
    if method in ("http", "streamable_http", "streamable-http"):
        return _is_sse_mcp_url(_resolve_mcp_url(svc))
    if method == "sse":
        return _is_sse_mcp_url(_resolve_mcp_url(svc))
    return False


class _CancelledError(Exception):
    pass


class _BuildFailedError(Exception):
    """智能构建阶段不可恢复的失败（如 LLM 不可用）。"""

    def __init__(self, reason: str, suggestion: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.suggestion = suggestion or "请检查 LLM 配置与网络后重试"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SimulationOrchestrator:
    """仿真编排器。async generator 产出 SimulationEvent。"""

    def __init__(self, cfg: dict[str, Any]):
        self.app_name: str = cfg.get("appName", "元应用")
        self.domain: str = cfg.get("domain", "generic")
        self.scenario_summary: str = cfg.get("scenarioSummary", "")
        self.scenario: str = cfg.get("scenarioDescription", "") or self.scenario_summary
        pre = cfg.get("scenarioParsed")
        self.scenario_parsed_pre: dict | None = pre if isinstance(pre, dict) and pre else None
        self.catalog_services_meta: list[dict] = cfg.get("servicesMeta", [])
        self.services_meta: list[dict] = list(self.catalog_services_meta)
        self.service_ids: list[str] = cfg.get("serviceIds", [])
        self.max_iterations: int = cfg.get("maxIterations", 5)
        self.mode: str = cfg.get("mode", "production")
        self.strategy: dict = cfg.get("strategy", {})

        self._cancelled = False
        self._started_at = 0.0
        self._final_iteration = 1
        self._build_succeeded = False

        self._domain_skill_name = f"domain_{self.domain}"

        self._tools = ToolRegistry()
        self._mcp_conn = MCPConnectionManager()
        self._sandbox_tools: list[SandboxTool] = []
        self._mcp_bindings: list[_McpServiceBinding] = []
        self._tool_names_used: set[str] = set()
        self._tools_ready = False
        self._scenario_parsed: dict | None = None
        self._service_selection_report: dict[str, Any] | None = None

    def cancel(self) -> None:
        self._cancelled = True

    def _min_iterations(self) -> int:
        """已废弃：不再用于强制重跑 Planner。保留读取以兼容旧 trace / 研究配置。"""
        raw = self.strategy.get("minIterations")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _stability_passes_required(self) -> int:
        """同一调度轨迹需连续通过 Verifier 的次数（初版可固化前的稳定性复检）。"""
        raw = self.strategy.get("stabilityPasses")
        if raw is None:
            return 1
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _strategy_value(self, key: str, default: str = "") -> str:
        return str(self.strategy.get(key, default) or default).strip().lower()

    def _sandbox_force_mock(self) -> bool:
        return self._strategy_value("sandbox") == "full_mock"

    def _sandbox_no_fallback(self) -> bool:
        return self._strategy_value("sandbox") == "none"

    def _repair_enabled(self) -> bool:
        return self._strategy_value("repair", "llm_repair") != "none"

    def _verification_mode(self) -> str:
        mode = self._strategy_value("verification", "multi_agent")
        if mode in ("multi_agent", "single_agent", "rule_based"):
            return mode
        return "multi_agent"

    # ====================== 工具注册（双通道） ======================

    async def _register_tools(self) -> None:
        """isFake=false 且已登记 SSE → 真实 MCP；否则 SandboxTool。"""
        self._tools = ToolRegistry()
        self._sandbox_tools = []
        self._mcp_bindings = []
        self._tool_names_used = set()
        self._tools.register(Terminate())

        for svc in self.services_meta:
            self._check_cancel()
            service_id = str(svc.get("id") or "")
            service_name = str(svc.get("name") or "?")
            mcp_url = _resolve_mcp_url(svc)

            if not self._sandbox_force_mock() and _should_use_real_mcp(svc):
                await self._register_mcp_service(svc, service_id, service_name, mcp_url)
            else:
                self._register_sandbox_service(svc, service_id, service_name)

        self._tools_ready = True

    def _server_config_for_service(
        self, svc: dict[str, Any], server_id: str, mcp_url: str
    ) -> ServerConfig:
        method = _mcp_transport_method(svc)
        if method == "stdio":
            return ServerConfig(
                connection_type="stdio",
                command=str(svc.get("mcpCommand")),
                args=list(svc.get("mcpArgs") or []),
                server_id=server_id,
            )
        if method in ("http", "streamable_http", "streamable-http"):
            return ServerConfig(
                connection_type="streamable_http",
                server_url=mcp_url,
                server_id=server_id,
            )
        return ServerConfig(
            connection_type="sse",
            server_url=mcp_url,
            server_id=server_id,
        )

    async def _register_mcp_service(
        self,
        svc: dict[str, Any],
        service_id: str,
        service_name: str,
        mcp_url: str,
    ) -> None:
        prefix = _sanitize(svc.get("id") or svc.get("name"))
        server_id = _sanitize(f"{prefix}_mcp")
        try:
            config = self._server_config_for_service(svc, server_id, mcp_url)
            _, mcp_tools = await self._mcp_conn.connect(config)
            logged: list[LoggingMCPTool] = []
            for inner in mcp_tools:
                alias = self._unique(prefix, inner.name)
                wrapper = LoggingMCPTool(
                    inner,
                    registered_name=alias,
                    service_id=service_id,
                    service_name=service_name,
                    transport=config.connection_type,
                )
                self._tools.register(wrapper)
                logged.append(wrapper)

            self._mcp_bindings.append(
                _McpServiceBinding(
                    service_id=service_id,
                    service_name=service_name,
                    server_id=server_id,
                    mcp_url=mcp_url,
                    tools=logged,
                )
            )
            logger.info(
                f"仿真 MCP 已连接 [{service_name}] {mcp_url} → "
                f"{[t.name for t in logged]}"
            )
        except Exception as exc:
            if self._sandbox_no_fallback():
                raise _BuildFailedError(
                    f"MCP 连接失败 [{service_name}] {mcp_url}: {exc}",
                    "策略为「无沙箱」时不回退模拟通道；请检查 MCP 服务或改为 CoW/全模拟",
                ) from exc
            logger.warning(
                f"MCP 连接失败 [{service_name}] {mcp_url}: {exc}，回退 SandboxTool"
            )
            self._register_sandbox_service(svc, service_id, service_name)

    def _register_sandbox_service(
        self,
        svc: dict[str, Any],
        service_id: str,
        service_name: str,
    ) -> None:
        prefix = _sanitize(svc.get("id") or svc.get("name"))
        tool_name = self._unique(prefix, "execute")
        tool = SandboxTool(
            name=tool_name,
            description=(
                f"调用服务 [{service_name}]："
                f"{svc.get('description', '执行该服务的核心功能（模拟通道）')}"
            ),
            service_id=service_id,
            service_name=service_name,
        )
        self._tools.register(tool)
        self._sandbox_tools.append(tool)

    # ====================== 主流程 ======================

    async def run(self) -> AsyncIterator[SimulationEvent]:
        self._started_at = time.time()
        try:
            scenario_parsed = await self._parse_scenario_parsed()
            self._scenario_parsed = scenario_parsed
            if scenario_parsed:
                yield SimulationEvent("scenario_parsed", scenario_parsed)

            async for e in self._phase_service_match():
                yield e
            async for e in self._phase_env_prep():
                yield e
            async for e in self._phase_intelligent_build():
                yield e

            if not self._build_succeeded:
                yield SimulationEvent("complete", {
                    "success": False,
                    "metrics": {"iterations": self._final_iteration, "elapsedMs": self._elapsed_ms()},
                    "result": {
                        "error": "智能构建未通过验证",
                        "suggestion": "已达最大迭代次数，仍未通过验证；请检查服务配置或调整场景描述",
                        "appName": self.app_name,
                        "domain": self.domain,
                        "executionPath": self._extract_execution_path(),
                        "toolChannels": self._tool_channel_summary(),
                    },
                })
                return

            async for e in self._phase_generation():
                yield e

            if self.mode == "research":
                for metric, value in self._collect_metrics().items():
                    yield SimulationEvent("metrics", {"metric": metric, "value": value})

            elapsed = self._elapsed_ms()
            exec_path = self._extract_execution_path()
            yield SimulationEvent("complete", {
                "success": True,
                "metrics": {"iterations": self._final_iteration, "elapsedMs": elapsed},
                "result": {
                    "executionPath": exec_path,
                    "strategy": self.strategy,
                    "appName": self.app_name,
                    "domain": self.domain,
                    "toolChannels": self._tool_channel_summary(),
                },
            })
        except _CancelledError:
            yield SimulationEvent("complete", {
                "success": False,
                "cancelled": True,
                "metrics": {"iterations": 0, "elapsedMs": self._elapsed_ms()},
                "result": {"error": "用户取消"},
            })
        except _BuildFailedError as exc:
            yield SimulationEvent("complete", {
                "success": False,
                "metrics": {"iterations": self._final_iteration, "elapsedMs": self._elapsed_ms()},
                "result": {"error": exc.reason, "suggestion": exc.suggestion},
            })
        except Exception as exc:
            logger.error(f"仿真异常: {exc}", exc_info=True)
            yield SimulationEvent("complete", {
                "success": False,
                "metrics": {"elapsedMs": self._elapsed_ms()},
                "result": {"error": str(exc), "suggestion": "请检查日志后重试"},
            })
        finally:
            await self._mcp_conn.disconnect_all()

    def _tool_channel_summary(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for b in self._mcp_bindings:
            rows.append({
                "serviceId": b.service_id,
                "name": b.service_name,
                "channel": "mcp",
                "mcpUrl": b.mcp_url,
            })
        for t in self._sandbox_tools:
            rows.append({
                "serviceId": t.service_id,
                "name": t.service_name,
                "channel": "sandbox",
                "mcpUrl": "",
            })
        return rows

    # ====================== Phase 0: 服务匹配 ======================

    async def _phase_service_match(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 0, "name": "服务匹配"})
        yield self._log("INFO", "开始服务匹配")

        self._service_selection_report = await self._select_services_from_catalog()
        yield SimulationEvent("service_selection", self._service_selection_report)
        selected_ids = {
            str(s.get("serviceId"))
            for s in self._service_selection_report.get("selectedServices", [])
            if s.get("serviceId")
        }
        if selected_ids:
            self.services_meta = [
                svc for svc in self.catalog_services_meta
                if str(svc.get("id") or "") in selected_ids
            ]
            self.service_ids = [str(svc.get("id") or "") for svc in self.services_meta]
        else:
            self.services_meta = list(self.catalog_services_meta)

        await self._register_tools()
        yield self._log(
            "INFO",
            f"服务选择完成：选中 {len(self.services_meta)} / {len(self.catalog_services_meta)} 个目录服务",
        )

        for binding in self._mcp_bindings:
            self._check_cancel()
            yield self._log("INFO", f"检测 MCP 服务: {binding.service_name}")
            t0 = time.time()
            try:
                session = self._mcp_conn.get_session(binding.server_id)
                if not session:
                    raise RuntimeError("MCP 会话不存在")
                resp = await session.list_tools()
                latency = int((time.time() - t0) * 1000)
                tool_names = [t.name for t in resp.tools]
                yield SimulationEvent("service", {
                    "id": binding.service_id,
                    "status": "online",
                    "latency": latency,
                    "channel": "mcp",
                    "tools": tool_names,
                })
                yield self._log(
                    "SUCCESS",
                    f"{binding.service_name} MCP 在线 ({latency}ms, {len(tool_names)} 个工具)",
                )
            except Exception as exc:
                yield SimulationEvent("service", {
                    "id": binding.service_id,
                    "status": "error",
                    "error": str(exc),
                    "channel": "mcp",
                })
                yield self._log("WARN", f"{binding.service_name} MCP 探测失败: {exc}")

        for tool in self._sandbox_tools:
            self._check_cancel()
            yield self._log("INFO", f"检测模拟服务: {tool.service_name}")

            try:
                probe = await tool.execute(action="health_check")
                if tool.call_log:
                    self._annotate_records(
                        [tool.call_log[-1]],
                        phase="service_matching",
                        purpose="service_probe",
                    )
                if probe.error:
                    yield SimulationEvent("service", {
                        "id": tool.service_id,
                        "status": "error",
                        "error": probe.error,
                        "channel": "sandbox",
                    })
                    yield self._log("WARN", f"{tool.service_name} 探测失败: {probe.error}")
                else:
                    latency = tool.call_log[-1].latency_ms if tool.call_log else 0
                    yield SimulationEvent("service", {
                        "id": tool.service_id,
                        "status": "online",
                        "latency": latency,
                        "channel": "sandbox",
                    })
                    yield self._log("SUCCESS", f"{tool.service_name} 模拟通道正常 ({latency}ms)")
            except Exception as exc:
                yield SimulationEvent("service", {
                    "id": tool.service_id,
                    "status": "error",
                    "error": str(exc),
                    "channel": "sandbox",
                })
                yield self._log("WARN", f"{tool.service_name} 探测异常: {exc}")

        yield self._log("SUCCESS", "服务匹配完成")

    async def _select_services_from_catalog(self) -> dict[str, Any]:
        """LLM service selection over the provided catalog only."""
        from micro_agent.simulation.artifact_compiler import SERVICE_SELECTION_SCHEMA

        services = list(self.catalog_services_meta)
        if not services:
            return {
                "schemaVersion": SERVICE_SELECTION_SCHEMA,
                "selectionId": "sel-empty",
                "strategy": "llm_catalog_selection",
                "selectedServices": [],
                "rejectedServices": [],
                "missingCapabilities": ["empty_service_catalog"],
                "rationale": "No services were provided.",
                "model": None,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }

        catalog = []
        for svc in services:
            catalog.append({
                "serviceId": str(svc.get("id") or ""),
                "serviceName": svc.get("name") or svc.get("id") or "",
                "description": svc.get("description") or svc.get("des") or "",
                "tools": [
                    {
                        "name": t.get("name") or t.get("id"),
                        "description": t.get("description") or t.get("des") or "",
                    }
                    for t in svc.get("tools") or []
                ],
            })
        system = (
            "你是元应用构建的服务匹配器。只允许从给定服务池中选择服务，"
            "不得发明、发现、下载或部署新服务。只输出 JSON。字段："
            "selectedServices[{serviceId,reason,matchedCapabilities[]}], "
            "rejectedServices[{serviceId,reason}], missingCapabilities[], rationale, confidence。"
        )
        user = {
            "scenario": self._scenario_parsed or {
                "goal": self.scenario_summary or self.scenario,
                "description": self.scenario,
                "domain": self.domain,
            },
            "serviceCatalog": catalog,
        }
        try:
            llm = LLM(config.llm)
            resp = await llm.complete([
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ])
            raw = self._parse_intent_json(resp.content or "") or {}
            report = self._normalize_service_selection(raw, services)
            report["model"] = llm.model
            return report
        except Exception as exc:
            logger.warning(f"LLM 服务匹配失败，回退使用传入 serviceIds/catalog: {exc}")
            return self._fallback_service_selection(services, reason=str(exc))

    def _normalize_service_selection(
        self,
        raw: dict[str, Any],
        services: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from micro_agent.simulation.artifact_compiler import SERVICE_SELECTION_SCHEMA, stable_hash

        known = {str(s.get("id") or ""): s for s in services}
        selected_rows = raw.get("selectedServices") or []
        selected_ids: list[str] = []
        selected = []
        for row in selected_rows:
            sid = str(row.get("serviceId") or row.get("id") or "")
            if sid not in known or sid in selected_ids:
                continue
            selected_ids.append(sid)
            selected.append({
                "serviceId": sid,
                "serviceName": known[sid].get("name") or sid,
                "reason": str(row.get("reason") or "LLM selected this service"),
                "matchedCapabilities": list(row.get("matchedCapabilities") or []),
            })
        if not selected:
            return self._fallback_service_selection(services, reason="llm_selected_none")

        rejected = []
        for sid, svc in known.items():
            if sid in selected_ids:
                continue
            reason = ""
            for row in raw.get("rejectedServices") or []:
                if str(row.get("serviceId") or row.get("id") or "") == sid:
                    reason = str(row.get("reason") or "")
                    break
            rejected.append({
                "serviceId": sid,
                "serviceName": svc.get("name") or sid,
                "reason": reason or "not selected for the current scenario",
            })
        return {
            "schemaVersion": SERVICE_SELECTION_SCHEMA,
            "selectionId": f"sel-{stable_hash(selected)[:16]}",
            "strategy": "llm_catalog_selection",
            "selectedServices": selected,
            "rejectedServices": rejected,
            "missingCapabilities": list(raw.get("missingCapabilities") or []),
            "rationale": str(raw.get("rationale") or ""),
            "confidence": raw.get("confidence"),
            "model": None,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }

    def _fallback_service_selection(
        self,
        services: list[dict[str, Any]],
        *,
        reason: str,
    ) -> dict[str, Any]:
        from micro_agent.simulation.artifact_compiler import SERVICE_SELECTION_SCHEMA, stable_hash

        requested = {str(x) for x in self.service_ids if x}
        selected = []
        rejected = []
        for svc in services:
            sid = str(svc.get("id") or "")
            row = {
                "serviceId": sid,
                "serviceName": svc.get("name") or sid,
                "reason": "fallback selected from requested serviceIds/catalog",
                "matchedCapabilities": [
                    str(t.get("name") or t.get("id"))
                    for t in svc.get("tools") or []
                    if t.get("name") or t.get("id")
                ],
            }
            if not requested or sid in requested:
                selected.append(row)
            else:
                rejected.append({
                    "serviceId": sid,
                    "serviceName": svc.get("name") or sid,
                    "reason": "not in requested serviceIds",
                })
        return {
            "schemaVersion": SERVICE_SELECTION_SCHEMA,
            "selectionId": f"sel-{stable_hash(selected)[:16]}",
            "strategy": "provided_catalog_fallback",
            "selectedServices": selected,
            "rejectedServices": rejected,
            "missingCapabilities": [],
            "rationale": f"Fallback selection because LLM selection was unavailable: {reason[:160]}",
            "model": None,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }

    # ====================== Phase 1: 环境准备 ======================

    async def _phase_env_prep(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 1, "name": "环境准备"})
        yield self._log("INFO", "开始准备仿真环境")

        steps = [
            ("初始化仿真运行时", self._init_runtime),
            ("加载服务配置", self._load_service_config),
        ]

        for i, (text, fn) in enumerate(steps):
            self._check_cancel()
            yield SimulationEvent("progress", {"ctx": "env", "index": i, "text": text, "active": True})
            await fn()
            yield SimulationEvent("progress", {"ctx": "env", "index": i, "text": text, "done": True})
            yield self._log("INFO", text)

        mcp_n = sum(len(b.tools) for b in self._mcp_bindings)
        yield self._log(
            "SUCCESS",
            f"环境准备完成 — MCP 工具 {mcp_n} 个，模拟工具 {len(self._sandbox_tools)} 个",
        )

    async def _init_runtime(self) -> None:
        names = [n for n in self._tools.list_names() if n != "terminate"]
        logger.debug(f"仿真运行时初始化: tools={names}")

    async def _load_service_config(self) -> None:
        logger.debug(f"服务配置加载: {len(self.services_meta)} 个服务")

    # ====================== Phase 2: 智能构建 ======================

    async def _phase_intelligent_build(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 2, "name": "智能构建"})
        yield self._log("INFO", "开始智能构建")
        if self.strategy:
            yield self._log(
                "INFO",
                f"研究策略: sandbox={self.strategy.get('sandbox', 'cow')} "
                f"planning={self.strategy.get('planning', 'llm_autonomous')} "
                f"verification={self.strategy.get('verification', 'multi_agent')} "
                f"repair={self.strategy.get('repair', 'llm_repair')} "
                f"solidify={self.strategy.get('solidify', 'golden_trace')}",
            )
            if self._strategy_value("planning") == "preset_workflow":
                yield self._log(
                    "WARN",
                    "preset_workflow 不是后端运行分支；本链路继续使用 LLM 自主规划。完整 demo 请使用前端已有 mock 路线。",
                )

        self._final_iteration = 1
        repair_planner_trace: list[AgentEvent] = []

        for iteration in range(1, self.max_iterations + 1):
            self._check_cancel()
            self._final_iteration = iteration
            call_offset = len(self._collect_call_records())
            yield SimulationEvent("iteration", {"iteration": iteration, "status": "running"})
            yield self._log("INFO", f"第 {iteration} 轮开始")

            yield SimulationEvent("phase", {"phase": "data", "status": "running"})
            yield self._log("INFO", "规划 Agent 执行中…")

            planner = self._build_planner(iteration, repair_planner_trace)
            current_planner_trace: list[AgentEvent] = []
            planner_had_error = False
            async for event in planner.run(self._planner_prompt(iteration, repair_planner_trace)):
                current_planner_trace.append(event)
                # 每次工具调用前后 emit service_calling，驱动前端画布动画
                if event.type == "tool_call":
                    svc = self._resolve_service_for_tool(event.data.get("tool", ""))
                    if svc:
                        yield SimulationEvent("service_calling", {
                            "serviceId": svc["serviceId"],
                            "serviceName": svc["serviceName"],
                            "toolName": event.data.get("tool", ""),
                            "status": "start",
                        })
                elif event.type == "tool_result":
                    svc = self._resolve_service_for_tool(event.data.get("tool", ""))
                    if svc:
                        yield SimulationEvent("service_calling", {
                            "serviceId": svc["serviceId"],
                            "serviceName": svc["serviceName"],
                            "toolName": event.data.get("tool", ""),
                            "status": "end",
                        })
                yield self._log("INFO", self._format_agent_event("Planner", event))
                if event.type == "error":
                    planner_had_error = True

            yield SimulationEvent("phase", {"phase": "data", "status": "done"})

            if planner_had_error:
                error_detail = self._extract_agent_error(current_planner_trace)
                yield self._log("ERROR", f"规划 Agent 异常: {error_detail}")
                if self._is_infra_error(error_detail):
                    raise _BuildFailedError(
                        f"规划 Agent 不可用: {error_detail}",
                        "请检查 LLM API Key / 网络配置后重试",
                    )
                yield SimulationEvent("issue", {
                    "iteration": iteration,
                    "message": f"规划失败: {error_detail}",
                    "fix": "下一轮重试",
                    "phase": "planning",
                })
                if not self._repair_enabled():
                    yield self._log("WARN", f"第 {iteration} 轮规划失败，策略禁用修复，终止构建")
                    break
                repair_planner_trace = current_planner_trace
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "retry"})
                yield self._log("WARN", f"第 {iteration} 轮规划失败，进入下一轮")
                continue

            iter_records = self._collect_call_records()[call_offset:]
            self._annotate_iteration_records(iter_records, iteration, current_planner_trace)
            planner_decision = self._build_planner_decision_payload(
                iteration, iter_records, current_planner_trace
            )
            yield SimulationEvent("planner_decision", planner_decision)

            async for e in self._emit_logic_simulation():
                yield e

            required_passes = self._stability_passes_required()
            passed = True
            issue = ""
            for stability_pass in range(1, required_passes + 1):
                if stability_pass > 1:
                    yield self._log(
                        "INFO",
                        f"稳定性复检 {stability_pass}/{required_passes}（同一调度轨迹，不重新规划）",
                    )
                async for ev in self._stream_verification(
                    current_planner_trace,
                    iteration,
                    stability_pass=stability_pass,
                    planner_decision=planner_decision,
                    iter_records=iter_records,
                ):
                    if isinstance(ev, tuple):
                        passed, issue = ev
                    else:
                        yield ev
                if not passed:
                    break

            if passed:
                self._build_succeeded = True
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "passed"})
                if required_passes > 1:
                    yield self._log(
                        "SUCCESS",
                        f"第 {iteration} 轮调度轨迹已通过 {required_passes} 次验证，可作为初版可固化方案",
                    )
                else:
                    yield self._log("SUCCESS", f"第 {iteration} 轮验证通过")
                break
            else:
                yield SimulationEvent("issue", {
                    "iteration": iteration,
                    "message": issue,
                    "fix": "下一轮自动修复",
                    "phase": "verification",
                    "plannerDecision": planner_decision,
                    "executionPath": planner_decision.get("executionPath", []),
                })
                repair_planner_trace = current_planner_trace
                if not self._repair_enabled():
                    yield self._log("WARN", f"第 {iteration} 轮发现问题: {issue}，策略禁用修复，终止构建")
                    break
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "retry"})
                yield self._log("WARN", f"第 {iteration} 轮发现问题: {issue}")

    async def _emit_logic_simulation(self) -> AsyncIterator[SimulationEvent]:
        """业务逻辑仿真：对照 Planner 已产生的工具调用做通道与结果核验（非独立 Agent）。"""
        yield SimulationEvent("phase", {"phase": "logic", "status": "running"})
        yield self._log("INFO", "业务逻辑仿真：核对工具调用、通道与编排顺序")

        mcp_service_ids = {b.service_id for b in self._mcp_bindings}
        records = self._collect_call_records()

        if not records:
            yield self._log(
                "WARN",
                "本轮 Planner 未调用任何服务工具；逻辑仿真仅检查编排结构",
            )
            await asyncio.sleep(0.4)
        else:
            for rec in records:
                self._check_cancel()
                channel = "mcp" if rec.service_id in mcp_service_ids else "sandbox"
                svc = next(
                    (s for s in self.services_meta if str(s.get("id")) == str(rec.service_id)),
                    None,
                )
                svc_name = svc.get("name", rec.service_id) if svc else rec.service_id
                service_id = str(rec.service_id) if rec.service_id else ""
                emit_calling = bool(service_id and service_id != "internal")
                if emit_calling:
                    yield SimulationEvent("service_calling", {
                        "serviceId": service_id,
                        "serviceName": svc_name,
                        "toolName": rec.tool_name or "",
                        "status": "start",
                    })
                if rec.error:
                    yield self._log(
                        "WARN",
                        f"逻辑核验 [{svc_name}] {rec.tool_name} ({channel}) 失败: "
                        f"{str(rec.error)[:120]}",
                    )
                else:
                    preview = (rec.result or "")[:80].replace("\n", " ")
                    yield self._log(
                        "INFO",
                        f"逻辑核验 [{svc_name}] {rec.tool_name} ({channel}, "
                        f"{rec.latency_ms}ms) → {preview}",
                    )
                await asyncio.sleep(0.55)
                if emit_calling:
                    yield SimulationEvent("service_calling", {
                        "serviceId": service_id,
                        "serviceName": svc_name,
                        "toolName": rec.tool_name or "",
                        "status": "end",
                    })

        yield SimulationEvent("phase", {"phase": "logic", "status": "done"})

    # ====================== Phase 3: 方案生成 ======================

    async def _phase_generation(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 3, "name": "方案生成"})
        yield self._log("INFO", "开始生成方案")

        call_records = self._collect_call_records()

        gen_steps = [
            ("提取执行路径", lambda: self._extract_execution_path()),
            ("生成服务调度配置", lambda: self._build_dispatch_config(call_records)),
        ]

        for i, (text, fn) in enumerate(gen_steps):
            self._check_cancel()
            yield SimulationEvent("progress", {"ctx": "generate", "index": i, "text": text, "active": True})
            fn()
            yield SimulationEvent("progress", {"ctx": "generate", "index": i, "text": text, "done": True})
            yield self._log("INFO", text)

        yield self._log("SUCCESS", "方案生成完成")

    # ====================== Agent 构建 ======================

    def _build_planner(self, iteration: int, prev_trace: list[AgentEvent]) -> Agent:
        agent = Agent(
            name="simulation_planner",
            llm=LLM(config.llm),
            tools=self._tools,
            system_prompt=self._planner_system_prompt(),
            next_step_prompt="根据当前进展决定下一步调用哪个服务工具；若已全部调用完毕请调用 terminate。",
            max_steps=20,
        )
        self._load_domain_skill(agent)
        return agent

    def _build_verifier(self) -> Agent:
        tools = ToolRegistry()
        tools.register(Terminate())

        agent = Agent(
            name="simulation_verifier",
            llm=LLM(config.llm),
            tools=tools,
            system_prompt=(
                "你是仿真验证智能体。审查规划智能体的执行轨迹，判断服务调用是否完整、"
                "数据流转是否合理、调用顺序是否正确。\n"
                "审查结束后必须调用 terminate，并填写结构化字段：\n"
                "- verdict: \"passed\" 或 \"failed\"\n"
                "- result: 简短审查摘要（失败时写明原因）"
            ),
            max_steps=5,
        )
        self._load_domain_skill(agent)
        return agent

    def _load_domain_skill(self, agent: Agent) -> None:
        if SkillRegistry.get(self._domain_skill_name):
            agent.load_skill(self._domain_skill_name)
        elif self._domain_skill_name != "domain_generic" and SkillRegistry.get("domain_generic"):
            agent.load_skill("domain_generic")

    # ====================== Prompt ======================

    def _planner_system_prompt(self) -> str:
        tool_lines: list[str] = []
        for name in sorted(self._tools.list_names()):
            if name == "terminate":
                continue
            tool = self._tools.get(name)
            desc = (getattr(tool, "description", None) or "")[:160]
            tool_lines.append(f"  - {name}: {desc}")

        svc_lines = "\n".join(
            f"  - {s.get('name', '?')} (id={s.get('id', '?')}, "
            f"通道={'mcp' if _should_use_real_mcp(s) else 'sandbox'})"
            for s in self.services_meta
        )
        return (
            f"你是仿真规划智能体，负责执行元应用「{self.app_name}」的服务编排。\n\n"
            f"领域: {self.domain}\n"
            f"场景: {self.scenario or '通用场景'}\n"
            f"参与服务:\n{svc_lines}\n\n"
            f"可用工具（请按名称调用）:\n"
            + ("\n".join(tool_lines) if tool_lines else "  （无）")
            + "\n\n"
            "请按合理业务顺序调用工具，观察返回；完成后调用 terminate 并摘要结果。"
        )

    def _planner_prompt(self, iteration: int, prev_trace: list[AgentEvent]) -> str:
        if iteration == 1 or not prev_trace:
            return f"请开始执行元应用「{self.app_name}」的服务编排，逐步调用各服务并验证数据流转。"
        summary = self._summarize_trace(prev_trace)
        return (
            f"上一轮执行存在问题，请根据以下轨迹摘要进行修正后重新执行：\n{summary}"
        )

    def _verifier_prompt(self, trace: list[AgentEvent]) -> str:
        summary = self._summarize_trace(trace)
        svc_names = [s.get("name", "?") for s in self.services_meta]
        scenario = self.scenario or "（未提供场景描述）"
        return (
            f"任务场景（需要完成的目标）:\n{scenario}\n\n"
            f"规划智能体为完成该任务产生的执行轨迹：\n{summary}\n\n"
            f"参与服务: {svc_names}\n"
            f"请判断：在该场景下，编排结果是否看起来已经完成了任务目标（允许基于日志与推理，不要求形式完美）。\n"
            f"并检查：1) 关键服务是否被合理调用 2) 数据流转是否合理 3) 调用顺序是否合理。\n"
            f"审查结束后调用 terminate(verdict=\"passed\"|\"failed\", result=\"审查摘要\")。"
        )

    def _build_planner_decision_payload(
        self,
        iteration: int,
        iter_records: list[ToolCallRecord],
        planner_trace: list[AgentEvent],
    ) -> dict[str, Any]:
        """当前轮规划快照：供 planner_decision / verifier_result / issue 共用。"""
        planner_content = ""
        for ev in planner_trace:
            if ev.type == "done":
                planner_content = ev.data.get("result", "")[:500]
                break
        return {
            "iteration": iteration,
            "candidate_tools": [n for n in self._tools.list_names() if n != "terminate"],
            "selected_tools": list(dict.fromkeys(r.tool_name for r in iter_records)),
            "reason": planner_content[:200],
            "executionPath": self._extract_execution_path_from_records(iter_records),
            "dispatch": self._build_dispatch_config(iter_records),
            "tool_call_details": [
                {
                    "call_id": r.call_id,
                    "tool": r.tool_name,
                    "service": r.service_name or r.service_id,
                    "channel": r.channel,
                    "transport": r.transport,
                    "arguments": r.arguments,
                    "result_preview": (r.result or "")[:200],
                    "error": r.error,
                    "latency_ms": r.latency_ms,
                    "success": r.success,
                    "timestamp": r.timestamp,
                }
                for r in iter_records
            ],
        }

    async def _stream_verification(
        self,
        planner_trace: list[AgentEvent],
        iteration: int,
        *,
        stability_pass: int = 1,
        planner_decision: dict[str, Any] | None = None,
        iter_records: list[ToolCallRecord] | None = None,
    ) -> AsyncIterator[SimulationEvent | tuple[bool, str]]:
        """对当前 planner_trace 执行一次验证，产出 phase/log/verifier_result 事件，末项为 (passed, issue)。"""
        yield SimulationEvent("phase", {"phase": "check", "status": "running"})
        verification_result = ""
        verifier_trace: list[AgentEvent] = []
        passed = False
        issue = ""

        if self._verification_mode() == "rule_based":
            yield self._log("INFO", "规则验证：检查工具调用是否全部成功")
            call_records = iter_records if iter_records is not None else self._collect_call_records()
            failed = [r for r in call_records if r.error or not r.success]
            if not call_records:
                passed, issue = False, "未执行任何工具调用"
            elif failed:
                passed, issue = False, f"{len(failed)} 次工具调用失败"
            else:
                passed, issue = True, ""
            verification_result = (
                "规则验证通过：全部工具调用成功"
                if passed
                else f"规则验证未通过：{issue}"
            )
        else:
            label = "验证 Agent 执行中…" if stability_pass == 1 else "稳定性复检：验证 Agent 审查同一轨迹…"
            yield self._log("INFO", label)
            verifier = self._build_verifier()
            async for event in verifier.run(self._verifier_prompt(planner_trace)):
                verifier_trace.append(event)
                if event.type == "done":
                    verification_result = event.data.get("result", "")
                yield self._log("INFO", self._format_agent_event("Verifier", event))

            verifier_had_error = any(e.type == "error" for e in verifier_trace)
            if verifier_had_error:
                error_detail = self._extract_agent_error(verifier_trace)
                yield self._log("ERROR", f"验证 Agent 异常: {error_detail}")
                if self._is_infra_error(error_detail):
                    raise _BuildFailedError(
                        f"验证 Agent 不可用: {error_detail}",
                        "请检查 LLM API Key / 网络配置后重试",
                    )
                yield SimulationEvent("phase", {"phase": "check", "status": "done"})
                yield (False, f"验证异常: {error_detail}")
                return
            passed, issue = self._resolve_verification(verifier_trace, verification_result)

        yield SimulationEvent("phase", {"phase": "check", "status": "done"})

        round_records = iter_records if iter_records is not None else self._collect_call_records()
        evidence_ids = [r.call_id for r in round_records if r.call_id]
        verifier_checks = []
        if passed:
            verifier_checks.append({
                "check": "overall_verification",
                "status": "PASSED",
                "evidence_refs": evidence_ids,
            })
        else:
            verifier_checks.append({
                "check": "overall_verification",
                "status": "FAILED",
                "issue": issue,
                "evidence_refs": evidence_ids,
            })
        result_payload: dict[str, Any] = {
            "iteration": iteration,
            "status": "PASSED" if passed else "FAILED",
            "summary": verification_result[:500] if verification_result else "",
            "reason": "" if passed else issue,
            "checks": verifier_checks,
            "issues": [] if passed else [{"description": issue, "evidence_refs": evidence_ids}],
        }
        if stability_pass > 1:
            result_payload["stabilityPass"] = stability_pass
            result_payload["sameTrajectory"] = True
        if planner_decision:
            result_payload["plannerDecision"] = planner_decision
        verdict = self._extract_verifier_verdict(verifier_trace)[0]
        if verdict:
            result_payload["verdict"] = verdict
        yield SimulationEvent("verifier_result", result_payload)
        yield (passed, issue)

    # ====================== 想定解析 ======================

    async def _parse_scenario_parsed(self) -> dict | None:
        """用 LLM 把自然语言场景描述解析为 ScenarioParsed（best-effort）。

        产出落入 scenario_parsed 事件 → trace，供 artifact_compiler 编译。
        对话阶段已产出 scenarioParsed 时直接复用；无场景描述、LLM 不可用或返回非法 JSON 时返回 None。
        """
        from micro_agent.scenario.schema import normalize_scenario_parsed

        if self.scenario_parsed_pre:
            return normalize_scenario_parsed(
                self.scenario_parsed_pre,
                raw_user_input=self.scenario,
                domain=self.domain,
            ).to_dict()

        if not self.scenario:
            return None

        svc_names = [s.get("name", "?") for s in self.services_meta]
        system_prompt = (
            "你是想定解析器。把用户的元应用场景描述解析为结构化想定。"
            "只输出 JSON 对象，不要任何解释或 markdown。字段：\n"
            "- goal: 字符串，一句话核心业务目标\n"
            "- description: 字符串，完整场景描述\n"
            "- constraints: 字符串数组，业务/合规/隐私/顺序约束（无则空数组）\n"
            "- acceptanceCriteria: 字符串数组，验收标准（可检查，非最终成败判定）\n"
            "- domain: 字符串，领域标识\n"
        )
        user_prompt = (
            f"领域: {self.domain}\n"
            f"场景描述: {self.scenario}\n"
            f"参与服务: {svc_names}\n"
            "请输出 JSON。"
        )

        try:
            llm = LLM(config.llm)
            resp = await llm.complete([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ])
        except Exception as exc:
            logger.warning(f"想定解析 LLM 调用失败 (non-fatal): {exc}")
            return None

        raw = self._parse_intent_json(resp.content or "")
        if not raw:
            logger.debug("想定解析未得到合法 JSON，跳过 scenarioParsed")
            return None

        parsed = normalize_scenario_parsed(
            raw,
            raw_user_input=self.scenario,
            parser_model=llm.model,
            parsed_at=datetime.now(timezone.utc).isoformat(),
            domain=self.domain,
        )
        return parsed.to_dict()

    @staticmethod
    def _parse_intent_json(text: str) -> dict | None:
        """从 LLM 返回文本中抽取首个 JSON 对象。"""
        if not text:
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    # ====================== 数据提取 ======================

    def _collect_call_records(self) -> list[ToolCallRecord]:
        records: list[ToolCallRecord] = []
        for tool in self._sandbox_tools:
            records.extend(tool.call_log)
        for binding in self._mcp_bindings:
            for tool in binding.tools:
                records.extend(tool.call_log)
        records.sort(key=lambda r: r.timestamp)
        return records

    @staticmethod
    def _annotate_records(
        records: list[ToolCallRecord],
        *,
        phase: str,
        purpose: str,
        iteration: int | None = None,
    ) -> None:
        for rec in records:
            rec.phase = rec.phase or phase
            rec.purpose = rec.purpose or purpose
            if iteration is not None and rec.iteration is None:
                rec.iteration = iteration
            if not rec.source:
                rec.source = rec.channel

    def _annotate_iteration_records(
        self,
        records: list[ToolCallRecord],
        iteration: int,
        planner_trace: list[AgentEvent],
    ) -> None:
        tool_events = [
            ev for ev in planner_trace
            if ev.type == "tool_call" and not self._is_terminate_tool(str(ev.data.get("tool", "")))
        ]
        for idx, rec in enumerate(records, start=1):
            if (rec.arguments or {}).get("action") == "health_check":
                self._annotate_records([rec], phase="service_matching", purpose="service_probe")
                continue
            rec.phase = rec.phase or "slow_mode"
            rec.purpose = rec.purpose or "react_action"
            rec.iteration = rec.iteration or iteration
            rec.action_id = rec.action_id or f"iter{iteration}-a{idx}"
            if idx <= len(tool_events):
                rec.react_step_id = rec.react_step_id or f"iter{iteration}-step{tool_events[idx - 1].step}"
            if not rec.source:
                rec.source = rec.channel

    def _resolve_service_for_tool(self, tool_name: str) -> dict | None:
        """从注册名解析对应的 service。用于 service_calling 事件。"""
        if not tool_name or tool_name == "terminate":
            return None
        for binding in self._mcp_bindings:
            for tool in binding.tools:
                if tool.name == tool_name:
                    return {"serviceId": binding.service_id, "serviceName": binding.service_name}
        for tool in self._sandbox_tools:
            if tool.name == tool_name:
                sid = getattr(tool, "service_id", "")
                sname = getattr(tool, "service_name", "")
                return {"serviceId": sid, "serviceName": sname}
        return None

    def _extract_execution_path_from_records(self, records: list[ToolCallRecord]) -> list[str]:
        """从给定调用记录子集构建执行路径（当前轮规划）。"""
        usable = [
            r for r in records
            if not r.error and r.arguments.get("action") != "health_check"
        ]
        if not usable:
            return []

        path = ["用户输入"]
        for rec in usable:
            svc = next(
                (s for s in self.services_meta if str(s.get("id")) == str(rec.service_id)),
                None,
            )
            if rec.service_id == "internal":
                path.append(rec.tool_name or "internal")
                continue
            svc_label = svc.get("name", rec.service_id) if svc else rec.service_id
            if rec.tool_name and rec.tool_name != svc_label:
                path.append(f"{svc_label} · {rec.tool_name}")
            else:
                path.append(svc_label)
        path.append("输出结果")
        return path

    def _extract_execution_path(self) -> list[str]:
        """全量调用链（跨轮次），用于 complete.result。"""
        records = [
            r for r in self._collect_call_records()
            if not r.error and r.arguments.get("action") != "health_check"
        ]
        return self._extract_execution_path_from_records(records)

    def _build_dispatch_config(self, records: list[ToolCallRecord]) -> dict:
        steps = []
        for rec in records:
            if not rec.error:
                steps.append({
                    "tool": rec.tool_name,
                    "service": rec.service_id,
                    "latency_ms": rec.latency_ms,
                })
        return {"steps": steps, "total_calls": len(records)}

    def _collect_metrics(self) -> dict[str, float]:
        records = self._collect_call_records()
        total = len(records)
        errors = sum(1 for r in records if r.error)
        success_rate = (total - errors) / total if total > 0 else 1.0

        return {
            "sandboxFidelity": round(success_rate, 3),
            "planningAccuracy": 1.0 if self._final_iteration == 1 else 0.0,
            "verificationAccuracy": -1,
            "repairEffectiveness": -1,
        }

    # ====================== 工具方法 ======================

    def _check_cancel(self) -> None:
        if self._cancelled:
            raise _CancelledError()

    def _elapsed_ms(self) -> int:
        return int((time.time() - self._started_at) * 1000)

    @staticmethod
    def _log(level: str, message: str) -> SimulationEvent:
        return SimulationEvent("log", {"level": level, "message": message})

    @staticmethod
    def _format_agent_event(role: str, event: AgentEvent) -> str:
        t = event.type
        if t == "think":
            thought = event.data.get("thought", "")[:120]
            return f"[{role}] 思考: {thought}"
        if t == "tool_call":
            return f"[{role}] 调用工具: {event.data.get('tool', '?')}"
        if t == "tool_result":
            return f"[{role}] 工具返回: {event.data.get('tool', '?')}"
        if t == "done":
            return f"[{role}] 完成"
        if t == "error":
            return f"[{role}] 错误: {event.data.get('error', '')}"
        return f"[{role}] {t}"

    @staticmethod
    def _summarize_trace(events: list[AgentEvent]) -> str:
        lines = []
        for e in events:
            if e.type == "think":
                lines.append(f"Step {e.step} 思考: {e.data.get('thought', '')[:200]}")
            elif e.type == "tool_call":
                lines.append(f"Step {e.step} 调用: {e.data.get('tool', '?')}")
            elif e.type == "tool_result":
                lines.append(f"Step {e.step} 结果: {e.data.get('result', '')[:200]}")
            elif e.type == "done":
                lines.append(f"Step {e.step} 完成: {e.data.get('result', '')[:200]}")
        return "\n".join(lines) or "(无轨迹)"

    @staticmethod
    def _is_terminate_tool(tool_name: str) -> bool:
        return str(tool_name or "").endswith("terminate")

    @staticmethod
    def _extract_verifier_verdict(trace: list[AgentEvent]) -> tuple[str | None, str]:
        """从 verifier 轨迹读取 terminate.verdict；返回 (passed|failed|None, summary)。"""
        for ev in reversed(trace):
            if ev.type == "done":
                verdict = ev.data.get("verdict")
                summary = str(ev.data.get("result", "") or "")
                if verdict in ("passed", "failed"):
                    return verdict, summary
                break
        for ev in reversed(trace):
            if ev.type != "tool_call" or not SimulationOrchestrator._is_terminate_tool(
                str(ev.data.get("tool", ""))
            ):
                continue
            args = ev.data.get("arguments") or {}
            verdict = args.get("verdict")
            if verdict in ("passed", "failed"):
                return str(verdict), str(args.get("result", "") or "")
            break
        return None, ""

    @staticmethod
    def _resolve_verification(trace: list[AgentEvent], fallback_text: str) -> tuple[bool, str]:
        verdict, summary = SimulationOrchestrator._extract_verifier_verdict(trace)
        if verdict == "passed":
            return True, ""
        if verdict == "failed":
            issue = (summary or fallback_text or "验证未通过").strip()
            return False, issue[:200]
        return SimulationOrchestrator._parse_verification(fallback_text)

    @staticmethod
    def _parse_verification(text: str) -> tuple[bool, str]:
        if not text:
            return False, "验证 Agent 未产生有效结论"
        stripped = text.strip()
        upper = stripped.upper()
        head = stripped[:120]
        head_upper = upper[:120]
        # Verifier 应按约定输出 PASSED；兼容首行/首段含 PASSED 或中文「验证通过」
        if re.search(r"\bPASSED\b", upper):
            return True, ""
        if re.match(r"^(验证通过|通过验证|审查通过|【审查通过】)", stripped):
            return True, ""
        if "验证通过" in head and "未通过" not in head and "FAILED" not in head_upper:
            return True, ""
        if (
            "审查通过" in head
            and "审查未通过" not in head
            and "未通过" not in head
            and "FAILED" not in head_upper
        ):
            return True, ""
        m = re.search(r"FAILED\s*[：:]\s*(.+)", text, re.DOTALL | re.IGNORECASE)
        if m:
            return False, m.group(1).strip()[:200]
        if re.match(r"^(验证未通过|未通过验证|审查未通过|失败|【审查未通过】)", stripped):
            return False, stripped[:200]
        return False, stripped[:200]

    @staticmethod
    def _extract_agent_error(trace: list[AgentEvent]) -> str:
        for e in trace:
            if e.type == "error":
                return e.data.get("error", "未知错误")[:300]
        return "未知错误"

    @staticmethod
    def _is_infra_error(error_text: str) -> bool:
        infra_keywords = (
            "AuthenticationError", "401", "403", "api_key", "Missing Authentication",
            "RateLimitError", "429", "quota", "billing",
        )
        lower = error_text.lower()
        return any(kw.lower() in lower for kw in infra_keywords)

    def _unique(self, prefix: str, name: str) -> str:
        base = _sanitize(f"{prefix}_{name}")
        alias = base
        n = 1
        while alias in self._tool_names_used:
            alias = f"{base}_{n}"
            n += 1
        self._tool_names_used.add(alias)
        return alias
