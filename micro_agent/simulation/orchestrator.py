"""元应用仿真构建：连接给定服务，执行 ReAct 规划，并由 Verifier 验收。"""

from __future__ import annotations

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
from micro_agent.simulation.service_tool_session import (
    ServiceConnectionError,
    ServiceToolSession,
)
from micro_agent.simulation.trace_records import ToolCallRecord
from micro_agent.tool.registry import ToolRegistry
from micro_agent.tool.terminate import Terminate

# 与 ioeb simulation_builder / inmemory mock 主步骤条索引一致
_ENV_PREP_TASKS = (
    "初始化构建会话",
    "加载课题服务契约",
    "准备结构化想定上下文",
)


@dataclass
class SimulationEvent:
    type: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "timestamp": self.timestamp}


class _CancelledError(Exception):
    pass


class _BuildFailedError(Exception):
    def __init__(self, reason: str, suggestion: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.suggestion = suggestion or "请检查 LLM 与 MCP 配置后重试"


class SimulationOrchestrator:
    """给定结构化想定和服务边界，产出可编译的真实调用轨迹。"""

    def __init__(self, cfg: dict[str, Any]):
        self.app_name = str(cfg.get("appName") or "元应用")
        self.domain = str(cfg.get("domain") or "generic")
        self.scenario = str(cfg.get("scenarioDescription") or "")
        raw_scenario = cfg.get("scenarioParsed")
        self.scenario_parsed = raw_scenario if isinstance(raw_scenario, dict) else {}
        self.services_meta = list(cfg.get("servicesMeta") or [])
        self.max_iterations = max(1, int(cfg.get("maxIterations") or 5))

        self._cancelled = False
        self._started_at = 0.0
        self._final_iteration = 0
        self._build_succeeded = False
        self._domain_skill_name = f"domain_{self.domain}"

        self._service_session = ServiceToolSession(self.services_meta)

    def cancel(self) -> None:
        self._cancelled = True

    async def run(self) -> AsyncIterator[SimulationEvent]:
        self._started_at = time.time()
        try:
            scenario = await self._parse_scenario_parsed()
            if scenario:
                yield SimulationEvent("scenario_parsed", scenario)

            async for event in self._prepare_services():
                yield event
            async for event in self._build():
                yield event

            success = self._build_succeeded
            result: dict[str, Any] = {
                "appName": self.app_name,
                "domain": self.domain,
                "executionPath": self._extract_execution_path(),
            }
            if not success:
                result.update({
                    "error": "智能构建未通过验证",
                    "suggestion": "请检查服务能力或调整场景描述",
                })
            yield self._complete(success, result)
        except _CancelledError:
            yield self._complete(False, {"error": "用户取消"}, cancelled=True)
        except _BuildFailedError as exc:
            yield self._complete(False, {"error": exc.reason, "suggestion": exc.suggestion})
        except ServiceConnectionError as exc:
            yield self._complete(False, {"error": exc.reason, "suggestion": exc.suggestion})
        except Exception as exc:
            logger.error(f"仿真异常: {exc}", exc_info=True)
            yield self._complete(False, {"error": str(exc), "suggestion": "请检查日志后重试"})
        finally:
            await self._service_session.close()

    async def _prepare_services(self) -> AsyncIterator[SimulationEvent]:
        if not self.services_meta:
            raise _BuildFailedError("未提供可调度服务", "请先完成服务推荐")

        yield SimulationEvent("step", {"step": 0, "name": "服务匹配"})
        await self._service_session.connect(self._check_cancel)
        for service in self._service_session.statuses():
            yield SimulationEvent("service", {
                "id": service["id"],
                "status": "online",
                "channel": service["channel"],
                "tools": service["tools"],
            })
        yield self._log("SUCCESS", f"已连接 {len(self.services_meta)} 个服务")
        yield SimulationEvent("step", {"step": 1, "name": "环境准备"})
        for index, text in enumerate(_ENV_PREP_TASKS):
            yield SimulationEvent("progress", {"ctx": "env", "index": index, "text": text, "active": True})
            yield SimulationEvent("progress", {"ctx": "env", "index": index, "text": text, "done": True})

    async def _build(self) -> AsyncIterator[SimulationEvent]:
        yield SimulationEvent("step", {"step": 2, "name": "智能构建"})
        previous_trace: list[AgentEvent] = []

        for iteration in range(1, self.max_iterations + 1):
            self._check_cancel()
            self._final_iteration = iteration
            call_offset = len(self.call_records())
            yield SimulationEvent("iteration", {"iteration": iteration, "status": "running"})
            yield SimulationEvent("phase", {"phase": "data", "status": "running"})

            trace: list[AgentEvent] = []
            planner = self._build_planner()
            async for event in planner.run(self._planner_prompt(iteration, previous_trace)):
                trace.append(event)
                if event.type in {"tool_call", "tool_result"}:
                    service = self._service_session.resolve_service(str(event.data.get("tool") or ""))
                    if service:
                        yield SimulationEvent("service_calling", {
                            **service,
                            "toolName": event.data.get("tool", ""),
                            "status": "start" if event.type == "tool_call" else "end",
                        })
                yield self._log("INFO", self._format_agent_event("Planner", event))
            yield SimulationEvent("phase", {"phase": "data", "status": "done"})

            if any(event.type == "error" for event in trace):
                error = self._extract_agent_error(trace)
                if self._is_infra_error(error):
                    raise _BuildFailedError(f"规划 Agent 不可用: {error}")
                previous_trace = trace
                yield SimulationEvent("issue", {
                    "iteration": iteration,
                    "message": f"规划失败: {error}",
                    "phase": "planning",
                })
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "retry"})
                continue

            records = self.call_records()[call_offset:]
            self._annotate_iteration_records(records, iteration, trace)
            decision = self._planner_decision(iteration, records, trace)
            yield SimulationEvent("planner_decision", decision)

            passed = False
            issue = ""
            async for event in self._verify(trace, iteration, records):
                if isinstance(event, tuple):
                    passed, issue = event
                else:
                    yield event
            if passed:
                self._build_succeeded = True
                yield SimulationEvent("iteration", {"iteration": iteration, "status": "passed"})
                break

            previous_trace = trace
            yield SimulationEvent("issue", {
                "iteration": iteration,
                "message": issue,
                "phase": "verification",
            })
            yield SimulationEvent("iteration", {"iteration": iteration, "status": "retry"})

    async def _verify(
        self,
        planner_trace: list[AgentEvent],
        iteration: int,
        records: list[ToolCallRecord],
    ) -> AsyncIterator[SimulationEvent | tuple[bool, str]]:
        yield SimulationEvent("phase", {"phase": "check", "status": "running"})
        trace: list[AgentEvent] = []
        result = ""
        verifier = self._build_verifier()
        async for event in verifier.run(self._verifier_prompt(planner_trace)):
            trace.append(event)
            if event.type == "done":
                result = str(event.data.get("result") or "")
            yield self._log("INFO", self._format_agent_event("Verifier", event))

        if any(event.type == "error" for event in trace):
            error = self._extract_agent_error(trace)
            if self._is_infra_error(error):
                raise _BuildFailedError(f"验证 Agent 不可用: {error}")
            passed, issue = False, f"验证异常: {error}"
        else:
            passed, issue = self._resolve_verification(trace, result)

        yield SimulationEvent("phase", {"phase": "check", "status": "done"})
        evidence = [record.call_id for record in records if record.call_id]
        yield SimulationEvent("verifier_result", {
            "iteration": iteration,
            "status": "PASSED" if passed else "FAILED",
            "summary": result[:500],
            "reason": "" if passed else issue,
            "evidenceRefs": evidence,
        })
        yield passed, issue

    def _build_planner(self) -> Agent:
        agent = Agent(
            name="simulation_planner",
            llm=LLM(config.llm),
            tools=self._service_session.tools,
            system_prompt=self._planner_system_prompt(),
            next_step_prompt="根据当前进展调用服务工具；完成后调用 terminate。",
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
                "审查执行轨迹是否完成任务目标。结束时必须调用 terminate，"
                "verdict 填 passed 或 failed，result 填简短理由。"
            ),
            max_steps=5,
        )
        self._load_domain_skill(agent)
        return agent

    def _load_domain_skill(self, agent: Agent) -> None:
        name = self._domain_skill_name
        if SkillRegistry.get(name):
            agent.load_skill(name)
        elif name != "domain_generic" and SkillRegistry.get("domain_generic"):
            agent.load_skill("domain_generic")

    def _planner_system_prompt(self) -> str:
        tools = []
        for name in sorted(self._service_session.tools.list_names()):
            if name == "terminate":
                continue
            tool = self._service_session.tools.get(name)
            tools.append(f"- {name}: {(getattr(tool, 'description', '') or '')[:160]}")
        services = "\n".join(
            f"- {service['name']} ({service['channel']})"
            for service in self._service_session.statuses()
        )
        return (
            f"你负责执行元应用「{self.app_name}」。\n领域: {self.domain}\n"
            f"场景: {self.scenario or '通用场景'}\n服务:\n{services}\n工具:\n"
            + "\n".join(tools)
            + "\n按业务顺序调用工具，观察结果，完成后调用 terminate。"
        )

    def _planner_prompt(self, iteration: int, previous: list[AgentEvent]) -> str:
        if iteration == 1 or not previous:
            return f"开始执行「{self.app_name}」，调用必要服务完成任务。"
        return f"上一轮未通过，请根据轨迹修正后重试：\n{self._summarize_trace(previous)}"

    def _verifier_prompt(self, trace: list[AgentEvent]) -> str:
        return (
            f"任务场景:\n{self.scenario}\n\n执行轨迹:\n{self._summarize_trace(trace)}\n\n"
            "判断任务是否完成、服务调用与数据流是否合理，然后调用 terminate。"
        )

    def _planner_decision(
        self,
        iteration: int,
        records: list[ToolCallRecord],
        trace: list[AgentEvent],
    ) -> dict[str, Any]:
        result = next(
            (str(event.data.get("result") or "") for event in trace if event.type == "done"),
            "",
        )
        return {
            "iteration": iteration,
            "candidate_tools": [name for name in self._service_session.tools.list_names() if name != "terminate"],
            "selected_tools": list(dict.fromkeys(record.tool_name for record in records)),
            "reason": result[:200],
        }

    async def _parse_scenario_parsed(self) -> dict | None:
        """构建只规范化已有想定；自然语言追问属于构建前入口。"""
        from micro_agent.scenario.schema import normalize_scenario_parsed

        raw = self.scenario_parsed or ({
            "goal": self.scenario,
            "description": self.scenario,
            "domain": self.domain,
        } if self.scenario else None)
        if not raw:
            return None
        return normalize_scenario_parsed(
            raw,
            raw_user_input=self.scenario,
            domain=self.domain,
        ).to_dict()

    def call_records(self) -> list[ToolCallRecord]:
        return self._service_session.records()

    def _annotate_iteration_records(
        self,
        records: list[ToolCallRecord],
        iteration: int,
        trace: list[AgentEvent],
    ) -> None:
        calls = [
            event for event in trace
            if event.type == "tool_call" and not self._is_terminate_tool(str(event.data.get("tool") or ""))
        ]
        for index, record in enumerate(records, start=1):
            record.phase = record.phase or "slow_mode"
            record.purpose = record.purpose or "react_action"
            record.iteration = record.iteration or iteration
            record.action_id = record.action_id or f"iter{iteration}-a{index}"
            if index <= len(calls):
                record.react_step_id = record.react_step_id or f"iter{iteration}-step{calls[index - 1].step}"
            record.source = record.source or record.channel

    def _extract_execution_path(self) -> list[str]:
        records = [record for record in self.call_records() if not record.error]
        if not records:
            return []
        path = ["用户输入"]
        path.extend(
            f"{record.service_name or record.service_id} · {record.tool_name}"
            for record in records
        )
        return [*path, "输出结果"]

    def _complete(
        self,
        success: bool,
        result: dict[str, Any],
        *,
        cancelled: bool = False,
    ) -> SimulationEvent:
        return SimulationEvent("complete", {
            "success": success,
            "cancelled": cancelled,
            "metrics": {"iterations": self._final_iteration, "elapsedMs": self._elapsed_ms()},
            "result": result,
        })

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
        if event.type == "think":
            return f"[{role}] 思考: {str(event.data.get('thought') or '')[:120]}"
        if event.type in {"tool_call", "tool_result"}:
            return f"[{role}] {event.type}: {event.data.get('tool', '?')}"
        if event.type == "error":
            return f"[{role}] 错误: {event.data.get('error', '')}"
        return f"[{role}] {event.type}"

    @staticmethod
    def _summarize_trace(events: list[AgentEvent]) -> str:
        lines = []
        for event in events:
            if event.type == "think":
                lines.append(f"Step {event.step} 思考: {str(event.data.get('thought') or '')[:200]}")
            elif event.type == "tool_call":
                lines.append(f"Step {event.step} 调用: {event.data.get('tool', '?')}")
            elif event.type in {"tool_result", "done"}:
                value = event.data.get("result", "")
                lines.append(f"Step {event.step} {event.type}: {str(value)[:200]}")
        return "\n".join(lines) or "(无轨迹)"

    @staticmethod
    def _is_terminate_tool(tool_name: str) -> bool:
        return str(tool_name or "").endswith("terminate")

    @staticmethod
    def _extract_verifier_verdict(trace: list[AgentEvent]) -> tuple[str | None, str]:
        for event in reversed(trace):
            if event.type == "done" and event.data.get("verdict") in {"passed", "failed"}:
                return str(event.data["verdict"]), str(event.data.get("result") or "")
            if event.type == "tool_call" and SimulationOrchestrator._is_terminate_tool(
                str(event.data.get("tool") or "")
            ):
                arguments = event.data.get("arguments") or {}
                if arguments.get("verdict") in {"passed", "failed"}:
                    return str(arguments["verdict"]), str(arguments.get("result") or "")
        return None, ""

    @staticmethod
    def _resolve_verification(trace: list[AgentEvent], fallback: str) -> tuple[bool, str]:
        verdict, summary = SimulationOrchestrator._extract_verifier_verdict(trace)
        if verdict == "passed":
            return True, ""
        if verdict == "failed":
            return False, (summary or fallback or "验证未通过")[:200]
        return SimulationOrchestrator._parse_verification(fallback)

    @staticmethod
    def _parse_verification(text: str) -> tuple[bool, str]:
        value = (text or "").strip()
        if not value:
            return False, "验证 Agent 未产生有效结论"
        head = value[:120]
        if re.search(r"\bPASSED\b", value, re.IGNORECASE) or (
            any(word in head for word in ("验证通过", "审查通过"))
            and "未通过" not in head
        ):
            return True, ""
        match = re.search(r"FAILED\s*[：:]\s*(.+)", value, re.DOTALL | re.IGNORECASE)
        return False, (match.group(1) if match else value).strip()[:200]

    @staticmethod
    def _extract_agent_error(trace: list[AgentEvent]) -> str:
        return next(
            (str(event.data.get("error") or "未知错误")[:300] for event in trace if event.type == "error"),
            "未知错误",
        )

    @staticmethod
    def _is_infra_error(error: str) -> bool:
        value = error.lower()
        return any(token in value for token in (
            "authenticationerror", "401", "403", "api_key", "missing authentication",
            "ratelimiterror", "429", "quota", "billing",
        ))

__all__ = ["SimulationEvent", "SimulationOrchestrator"]
