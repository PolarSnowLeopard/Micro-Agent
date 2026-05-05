"""仿真编排器：4 阶段流程 + Planner/Verifier 双 Agent 核心循环。

输出 SimulationEvent 流，事件名与前端 simulation_builder.vue 一一对应：
  step / service / iteration / phase / issue / log / metrics / progress / complete

工具层：
  - 当前使用 SandboxTool（拟真 mock，接口与 MCPTool 一致）
  - 替换为真实 MCP 只需改 _register_tools() 的工具来源
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
from micro_agent.simulation.sandbox_tool import SandboxTool, ToolCallRecord
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


def _sanitize(text: str, max_len: int = 128) -> str:
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "srv"))
    return re.sub(r"_+", "_", ident).strip("_")[:max_len] or "srv"


class _CancelledError(Exception):
    pass


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

        self._domain_skill_name = f"domain_{self.domain}"

        self._tools = ToolRegistry()
        self._sandbox_tools: list[SandboxTool] = []
        self._register_tools()

    # ====================== 工具注册 ======================

    def _register_tools(self) -> None:
        """注册服务工具。当前用 SandboxTool；换真实 MCP 改此处即可。"""
        self._tools.register(Terminate())

        used: set[str] = set()
        for svc in self.services_meta:
            prefix = _sanitize(svc.get("id") or svc.get("name"))
            tool_name = self._unique(prefix, "execute", used)
            tool = SandboxTool(
                name=tool_name,
                description=f"调用服务 [{svc.get('name', '?')}]：{svc.get('description', '执行该服务的核心功能')}",
                service_id=svc.get("id", ""),
                service_name=svc.get("name", ""),
            )
            self._tools.register(tool)
            self._sandbox_tools.append(tool)

    def cancel(self) -> None:
        self._cancelled = True

    # ====================== 主流程 ======================

    async def run(self) -> AsyncIterator[SimulationEvent]:
        self._started_at = time.time()

        try:
            async for e in self._phase_service_match():
                yield e
            async for e in self._phase_env_prep():
                yield e
            async for e in self._phase_intelligent_build():
                yield e
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
                },
            })
        except _CancelledError:
            yield SimulationEvent("complete", {
                "success": False,
                "cancelled": True,
                "metrics": {"iterations": 0, "elapsedMs": self._elapsed_ms()},
                "result": {"error": "用户取消"},
            })
            return
        except Exception as exc:
            logger.error(f"仿真异常: {exc}", exc_info=True)
            yield SimulationEvent("complete", {
                "success": False,
                "metrics": {"elapsedMs": self._elapsed_ms()},
                "result": {"error": str(exc), "suggestion": "请检查日志后重试"},
            })
            return

    # ====================== Phase 0: 服务匹配 ======================

    async def _phase_service_match(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 0, "name": "服务匹配"})
        yield self._log("INFO", "开始服务匹配")

        for tool in self._sandbox_tools:
            self._check_cancel()
            yield self._log("INFO", f"检测服务: {tool.service_name}")

            try:
                probe = await tool.execute(action="health_check")
                if probe.error:
                    yield SimulationEvent("service", {
                        "id": tool.service_id,
                        "status": "error",
                        "error": probe.error,
                    })
                    yield self._log("WARN", f"{tool.service_name} 探测失败: {probe.error}")
                else:
                    latency = tool.call_log[-1].latency_ms if tool.call_log else 0
                    yield SimulationEvent("service", {
                        "id": tool.service_id,
                        "status": "online",
                        "latency": latency,
                    })
                    yield self._log("SUCCESS", f"{tool.service_name} 连接正常 ({latency}ms)")
            except Exception as exc:
                yield SimulationEvent("service", {
                    "id": tool.service_id,
                    "status": "error",
                    "error": str(exc),
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

        yield self._log("SUCCESS", f"环境准备完成 — {len(self._sandbox_tools)} 个工具已就绪")

    async def _init_runtime(self) -> None:
        tool_names = [t.name for t in self._sandbox_tools]
        logger.debug(f"仿真运行时初始化: tools={tool_names}")

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

            # --- Planner ---
            yield SimulationEvent("phase", {"phase": "data", "status": "running"})
            yield self._log("INFO", "规划 Agent 执行中…")

            planner = self._build_planner(iteration, planner_trace)
            planner_trace = []
            async for event in planner.run(self._planner_prompt(iteration, planner_trace)):
                planner_trace.append(event)
                yield self._log("INFO", self._format_agent_event("Planner", event))

            yield SimulationEvent("phase", {"phase": "data", "status": "done"})

            # --- Verifier ---
            yield SimulationEvent("phase", {"phase": "check", "status": "running"})
            yield self._log("INFO", "验证 Agent 执行中…")

            verifier = self._build_verifier()
            verification_result = ""
            async for event in verifier.run(self._verifier_prompt(planner_trace)):
                if event.type == "done":
                    verification_result = event.data.get("result", "")
                yield self._log("INFO", self._format_agent_event("Verifier", event))

            yield SimulationEvent("phase", {"phase": "check", "status": "done"})

            passed, issue = self._parse_verification(verification_result)

            if passed:
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "passed"})
                yield self._log("SUCCESS", f"第 {iteration} 轮验证通过")
                break
            else:
                yield SimulationEvent("issue", {"message": issue, "fix": "下一轮自动修复"})
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "retry"})
                yield self._log("WARN", f"第 {iteration} 轮发现问题: {issue}")

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
        """按 domain 加载领域 Skill；找不到则回退到 domain_generic。"""
        if SkillRegistry.get(self._domain_skill_name):
            agent.load_skill(self._domain_skill_name)
        elif self._domain_skill_name != "domain_generic" and SkillRegistry.get("domain_generic"):
            agent.load_skill("domain_generic")

    # ====================== Prompt ======================

    def _planner_system_prompt(self) -> str:
        svc_list = "\n".join(
            f"  - {s.get('name', '?')}（工具名: {_sanitize(s.get('id') or s.get('name'))}_execute）"
            for s in self.services_meta
        )
        return (
            f"你是仿真规划智能体，负责执行元应用「{self.app_name}」的服务编排。\n\n"
            f"领域: {self.domain}\n"
            f"场景: {self.scenario or '通用场景'}\n"
            f"可用服务:\n{svc_list}\n\n"
            f"请按照合理的业务顺序逐一调用各服务工具，观察返回结果，"
            f"确认数据在服务间正确流转。\n"
            f"完成所有调用后，调用 terminate 并返回执行结果摘要。"
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
        return (
            f"以下是规划智能体的执行轨迹：\n{summary}\n\n"
            f"需要验证的服务列表: {svc_names}\n"
            f"请检查：1) 所有服务是否被调用 2) 数据流转是否合理 3) 调用顺序是否正确\n"
            f"结论为 PASSED 或 FAILED: [原因]，然后 terminate。"
        )

    # ====================== 数据提取 ======================

    def _collect_call_records(self) -> list[ToolCallRecord]:
        records = []
        for tool in self._sandbox_tools:
            records.extend(tool.call_log)
        records.sort(key=lambda r: r.timestamp)
        return records

    def _extract_execution_path(self) -> list[str]:
        """从实际工具调用日志中提取执行路径。"""
        records = self._collect_call_records()
        if not records:
            return ["用户输入"] + [s.get("name", "?") for s in self.services_meta] + ["输出结果"]

        seen: set[str] = set()
        path = ["用户输入"]
        for rec in records:
            if rec.service_id not in seen and not rec.error:
                seen.add(rec.service_id)
                svc = next(
                    (s for s in self.services_meta if s.get("id") == rec.service_id),
                    None,
                )
                path.append(svc.get("name", rec.service_id) if svc else rec.service_id)
        path.append("输出结果")
        return path

    def _build_dispatch_config(self, records: list[ToolCallRecord]) -> dict:
        """从调用记录构建调度配置（供后续编译使用）。"""
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
        """从实际执行中收集指标。

        当前部分指标仍需人工标注/后续接入真实度量，此处基于调用日志
        计算可计算的指标，其余标记为估计值。
        """
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
            return True, ""
        upper = text.upper()
        if "PASSED" in upper:
            return True, ""
        m = re.search(r"FAILED\s*[：:]\s*(.+)", text, re.DOTALL)
        issue = m.group(1).strip()[:200] if m else text[:200]
        return False, issue

    @staticmethod
    def _unique(prefix: str, name: str, used: set[str]) -> str:
        base = _sanitize(f"{prefix}_{name}")
        alias = base
        n = 1
        while alias in used:
            alias = f"{base}_{n}"
            n += 1
        used.add(alias)
        return alias
