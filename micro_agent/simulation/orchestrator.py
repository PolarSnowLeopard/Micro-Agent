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
        self.scenario: str = cfg.get("scenarioDescription", "")
        self.services_meta: list[dict] = cfg.get("servicesMeta", [])
        self.service_ids: list[str] = cfg.get("serviceIds", [])
        self.max_iterations: int = cfg.get("maxIterations", 3)
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

    def cancel(self) -> None:
        self._cancelled = True

    def _min_iterations(self) -> int:
        raw = self.strategy.get("minIterations")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

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

            if _should_use_real_mcp(svc):
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
            await self._register_tools()

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

        self._final_iteration = 1
        planner_trace: list[AgentEvent] = []

        for iteration in range(1, self.max_iterations + 1):
            self._check_cancel()
            self._final_iteration = iteration
            yield SimulationEvent("iteration", {"iteration": iteration, "status": "running"})
            yield self._log("INFO", f"第 {iteration} 轮开始")

            yield SimulationEvent("phase", {"phase": "data", "status": "running"})
            yield self._log("INFO", "规划 Agent 执行中…")

            planner = self._build_planner(iteration, planner_trace)
            planner_trace = []
            planner_had_error = False
            async for event in planner.run(self._planner_prompt(iteration, planner_trace)):
                planner_trace.append(event)
                yield self._log("INFO", self._format_agent_event("Planner", event))
                if event.type == "error":
                    planner_had_error = True

            yield SimulationEvent("phase", {"phase": "data", "status": "done"})

            if planner_had_error:
                error_detail = self._extract_agent_error(planner_trace)
                yield self._log("ERROR", f"规划 Agent 异常: {error_detail}")
                if self._is_infra_error(error_detail):
                    raise _BuildFailedError(
                        f"规划 Agent 不可用: {error_detail}",
                        "请检查 LLM API Key / 网络配置后重试",
                    )
                yield SimulationEvent("issue", {"message": f"规划失败: {error_detail}", "fix": "下一轮重试"})
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "retry"})
                yield self._log("WARN", f"第 {iteration} 轮规划失败，进入下一轮")
                continue

            # P0-3: Emit planner_decision event with structured decision trace
            iter_records = self._collect_call_records()
            selected_tools = list(dict.fromkeys(r.tool_name for r in iter_records))
            candidate_tools = [n for n in self._tools.list_names() if n != "terminate"]
            exec_path = self._extract_execution_path()
            dispatch_config = self._build_dispatch_config(iter_records)
            planner_content = ""
            for ev in planner_trace:
                if ev.type == "done":
                    planner_content = ev.data.get("result", "")[:500]
                    break
            yield SimulationEvent("planner_decision", {
                "iteration": iteration,
                "candidate_tools": candidate_tools,
                "selected_tools": selected_tools,
                "reason": planner_content[:200],
                "executionPath": exec_path,
                "dispatch": dispatch_config,
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
            })

            async for e in self._emit_logic_simulation():
                yield e

            yield SimulationEvent("phase", {"phase": "check", "status": "running"})
            yield self._log("INFO", "验证 Agent 执行中…")

            verifier = self._build_verifier()
            verification_result = ""
            verifier_trace: list[AgentEvent] = []
            async for event in verifier.run(self._verifier_prompt(planner_trace)):
                verifier_trace.append(event)
                if event.type == "done":
                    verification_result = event.data.get("result", "")
                yield self._log("INFO", self._format_agent_event("Verifier", event))

            yield SimulationEvent("phase", {"phase": "check", "status": "done"})

            verifier_had_error = any(e.type == "error" for e in verifier_trace)
            if verifier_had_error:
                error_detail = self._extract_agent_error(verifier_trace)
                yield self._log("ERROR", f"验证 Agent 异常: {error_detail}")
                if self._is_infra_error(error_detail):
                    raise _BuildFailedError(
                        f"验证 Agent 不可用: {error_detail}",
                        "请检查 LLM API Key / 网络配置后重试",
                    )
                yield SimulationEvent("issue", {"message": f"验证异常: {error_detail}", "fix": "下一轮重试"})
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "retry"})
                continue

            passed, issue = self._parse_verification(verification_result)

            # P0-1: Emit structured verifier_result event
            call_records = self._collect_call_records()
            evidence_ids = [r.call_id for r in call_records if r.call_id]
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
            yield SimulationEvent("verifier_result", {
                "iteration": iteration,
                "status": "PASSED" if passed else "FAILED",
                "summary": verification_result[:500] if verification_result else "",
                "reason": "" if passed else issue,
                "checks": verifier_checks,
                "issues": [] if passed else [{"description": issue, "evidence_refs": evidence_ids}],
            })

            if passed:
                min_iter = self._min_iterations()
                if iteration < min_iter:
                    yield self._log(
                        "INFO",
                        f"第 {iteration} 轮验证通过，继续下一轮"
                        f"（最少 {min_iter} 轮）",
                    )
                    yield SimulationEvent(
                        "iteration", {"iteration": iteration, "status": "retry"}
                    )
                    continue
                self._build_succeeded = True
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "passed"})
                yield self._log("SUCCESS", f"第 {iteration} 轮验证通过")
                break
            else:
                yield SimulationEvent("issue", {"message": issue, "fix": "下一轮自动修复"})
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
                await asyncio.sleep(0.25)

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
                "如果一切正常，回复 PASSED 并调用 terminate。\n"
                "如果有问题，回复 FAILED: [问题描述]，然后调用 terminate。"
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
            f"结论必须写为 PASSED 或 FAILED: [原因]，然后 terminate。"
        )

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

    def _extract_execution_path(self) -> list[str]:
        """按调用顺序列出每一步（不去重），反映真实调用链。"""
        records = [
            r for r in self._collect_call_records()
            if not r.error and r.arguments.get("action") != "health_check"
        ]
        if not records:
            return []

        path = ["用户输入"]
        for rec in records:
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
    def _parse_verification(text: str) -> tuple[bool, str]:
        if not text:
            return False, "验证 Agent 未产生有效结论"
        upper = text.upper()
        if "PASSED" in upper:
            return True, ""
        m = re.search(r"FAILED\s*[：:]\s*(.+)", text, re.DOTALL)
        issue = m.group(1).strip()[:200] if m else text[:200]
        return False, issue

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
