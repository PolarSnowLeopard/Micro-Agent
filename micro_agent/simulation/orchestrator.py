"""仿真编排器：4 阶段流程 + Planner/Verifier 双 Agent 核心循环。

输出 SimulationEvent 流，事件名与前端 simulation_builder.vue 一一对应：
  step / service / iteration / phase / issue / log / metrics / progress / complete

设计要点：
  - Planner 使用 SimulatedMCPTool（mock），后续替换为真实 MCP 只需改 tool 注册
  - Verifier 纯 LLM 推理，无工具
  - 两个 Agent 均复用 Agent.run() 的 ReAct 引擎
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
from micro_agent.tool.registry import ToolRegistry
from micro_agent.tool.simulated_mcp import SimulatedMCPTool
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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

ENV_TASKS = [
    "初始化仿真运行时",
    "加载服务配置",
    "配置沙箱隔离层",
    "准备数据注入通道",
]

GEN_TASKS = [
    "编译执行方案",
    "生成服务调度配置",
    "写入元应用描述",
]


def _sanitize(text: str, max_len: int = 128) -> str:
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "srv"))
    return re.sub(r"_+", "_", ident).strip("_")[:max_len] or "srv"


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

    def cancel(self) -> None:
        self._cancelled = True

    # ----- 主流程 -----

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

    # ----- Phase 0: 服务匹配 -----

    async def _phase_service_match(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 0, "name": "服务匹配"})
        yield self._log("INFO", "开始服务匹配")

        for svc in self.services_meta:
            self._check_cancel()
            await asyncio.sleep(0.3)
            yield self._log("INFO", f"检测服务: {svc.get('name', '?')}")
            yield SimulationEvent("service", {
                "id": svc.get("id", ""),
                "status": "online",
                "latency": 120,
            })
            yield self._log("SUCCESS", f"{svc.get('name', '?')} 连接正常 (120ms)")

        yield self._log("SUCCESS", "服务匹配完成")

    # ----- Phase 1: 环境准备 -----

    async def _phase_env_prep(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 1, "name": "环境准备"})
        yield self._log("INFO", "开始准备仿真环境")

        for i, text in enumerate(ENV_TASKS):
            self._check_cancel()
            yield SimulationEvent("progress", {"ctx": "env", "index": i, "text": text, "active": True})
            await asyncio.sleep(0.4)
            yield SimulationEvent("progress", {"ctx": "env", "index": i, "text": text, "done": True})
            yield self._log("INFO", text)

        yield self._log("SUCCESS", "环境准备完成")

    # ----- Phase 2: 智能构建（核心双 Agent 循环）-----

    async def _phase_intelligent_build(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 2, "name": "智能构建"})
        yield self._log("INFO", "开始智能构建")

        final_iteration = 1
        planner_trace: list[AgentEvent] = []

        for iteration in range(1, self.max_iterations + 1):
            self._check_cancel()
            final_iteration = iteration
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
            yield SimulationEvent("phase", {"phase": "logic", "status": "running"})
            await asyncio.sleep(0.2)
            yield SimulationEvent("phase", {"phase": "logic", "status": "done"})
            yield self._log("SUCCESS", "数据 & 逻辑仿真完成")

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

        # 研究模式：输出指标
        elapsed = self._elapsed_ms()
        if self.mode == "research":
            for metric, value in self._fake_module_metrics(final_iteration).items():
                yield SimulationEvent("metrics", {"metric": metric, "value": value})

        # complete
        exec_path = ["用户输入"] + [s.get("name", "?") for s in self.services_meta] + ["输出结果"]
        yield SimulationEvent("complete", {
            "success": True,
            "metrics": {"iterations": final_iteration, "elapsedMs": elapsed},
            "result": {
                "executionPath": exec_path,
                "strategy": self.strategy,
                "appName": self.app_name,
                "domain": self.domain,
            },
        })

    # ----- Phase 3: 方案生成 -----

    async def _phase_generation(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 3, "name": "方案生成"})
        yield self._log("INFO", "开始生成方案")

        for i, text in enumerate(GEN_TASKS):
            self._check_cancel()
            yield SimulationEvent("progress", {"ctx": "generate", "index": i, "text": text, "active": True})
            await asyncio.sleep(0.4)
            yield SimulationEvent("progress", {"ctx": "generate", "index": i, "text": text, "done": True})
            yield self._log("INFO", text)

        yield self._log("SUCCESS", "方案生成完成")

    # ====================== Agent 构建 ======================

    def _build_planner(self, iteration: int, prev_trace: list[AgentEvent]) -> Agent:
        llm = LLM(config.llm)
        tools = ToolRegistry()
        tools.register(Terminate())

        used: set[str] = set()
        for svc in self.services_meta:
            prefix = _sanitize(svc.get("id") or svc.get("name"))
            tool_name = self._unique(prefix, "execute", used)
            tools.register(SimulatedMCPTool(
                name=tool_name,
                description=f"调用服务 [{svc.get('name', '?')}] 的模拟接口",
                node_id=svc.get("id"),
                node_name=svc.get("name"),
                node_des=svc.get("name"),
            ))

        return Agent(
            name="simulation_planner",
            llm=llm,
            tools=tools,
            system_prompt=self._planner_system_prompt(),
            next_step_prompt="根据当前进展决定下一步调用哪个服务工具；若已全部调用完毕请调用 terminate。",
            max_steps=20,
        )

    def _build_verifier(self) -> Agent:
        llm = LLM(config.llm)
        tools = ToolRegistry()
        tools.register(Terminate())

        return Agent(
            name="simulation_verifier",
            llm=llm,
            tools=tools,
            system_prompt=(
                "你是仿真验证智能体。审查规划智能体的执行轨迹，判断服务调用是否完整、"
                "数据流转是否合理、调用顺序是否正确。\n"
                "如果一切正常，回复 PASSED 并调用 terminate。\n"
                "如果有问题，回复 FAILED: [问题描述]，然后调用 terminate。"
            ),
            max_steps=5,
        )

    # ====================== Prompt 构建 ======================

    def _planner_system_prompt(self) -> str:
        svc_list = "\n".join(
            f"  - {s.get('name', '?')}" for s in self.services_meta
        )
        return (
            f"你是仿真规划智能体，负责模拟执行元应用「{self.app_name}」的服务编排。\n\n"
            f"领域: {self.domain}\n"
            f"场景: {self.scenario or '通用场景'}\n"
            f"可用服务:\n{svc_list}\n\n"
            f"请按照合理的顺序逐一调用各服务工具，验证数据流转是否正确。\n"
            f"完成后调用 terminate 返回执行结果摘要。"
        )

    def _planner_prompt(self, iteration: int, prev_trace: list[AgentEvent]) -> str:
        if iteration == 1 or not prev_trace:
            return f"请开始仿真执行元应用「{self.app_name}」，逐步调用各服务并验证数据流转。"
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

    @staticmethod
    def _fake_module_metrics(iterations: int) -> dict[str, float]:
        base = 0.85 + min(iterations, 3) * 0.03
        return {
            "sandboxFidelity": round(base + 0.02, 3),
            "planningAccuracy": round(base, 3),
            "verificationAccuracy": round(base + 0.01, 3),
            "repairEffectiveness": round(base - 0.03, 3),
        }


class _CancelledError(Exception):
    pass
