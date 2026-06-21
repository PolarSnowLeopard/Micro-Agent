"""Runtime for locally running a compiled MetaAppArtifact."""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from micro_agent.core.agent import Agent
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.core.meta_app_agent import MetaAppAgent
from micro_agent.simulation.orchestrator import SimulationOrchestrator
from micro_agent.simulation.trace_records import build_tool_call_record_events


async def run_artifact(
    artifact: dict[str, Any],
    message: str,
    *,
    prefer_golden_path: bool = True,
) -> dict[str, Any]:
    """Run a meta-app artifact once.

    GoldenPath is attempted first when available. It is an internal runtime
    asset, not an external router: an LLM decides applicability and emits a
    structured BindingPlan. If the fast path fails, slow-mode Agent execution
    is used.
    """
    started = time.time()
    fast_result = None
    if prefer_golden_path and artifact.get("goldenPaths"):
        fast_result = await _try_golden_path(artifact, message)
        if fast_result.get("success"):
            return {
                "schemaVersion": "artifact_run_result.v1",
                "artifactId": artifact.get("artifactId"),
                "mode": "golden_path",
                "success": True,
                "fastPathSuccess": True,
                "fallbackUsed": False,
                "latencyMs": int((time.time() - started) * 1000),
                "result": fast_result.get("result"),
                "bindingPlan": fast_result.get("bindingPlan"),
                "toolCalls": fast_result.get("toolCalls") or [],
            }

    slow_result = await _run_slow_mode(artifact, message)
    return {
        "schemaVersion": "artifact_run_result.v1",
        "artifactId": artifact.get("artifactId"),
        "mode": "slow_mode_after_fallback" if fast_result else "slow_mode",
        "success": bool(slow_result.get("success")),
        "fastPathSuccess": False,
        "fallbackUsed": bool(fast_result),
        "fastPathError": fast_result.get("error") if fast_result else None,
        "latencyMs": int((time.time() - started) * 1000),
        "result": slow_result.get("result"),
        "events": slow_result.get("events") or [],
    }


async def _try_golden_path(artifact: dict[str, Any], message: str) -> dict[str, Any]:
    path = next((p for p in artifact.get("goldenPaths") or [] if p.get("primary")), None)
    if not path:
        return {"success": False, "error": "missing_primary_golden_path"}

    binding_plan = await _build_binding_plan(artifact, path, message)
    if not binding_plan.get("useGoldenPath", True):
        return {"success": False, "error": binding_plan.get("reason") or "agent_declined_golden_path", "bindingPlan": binding_plan}

    missing = [
        slot.get("name")
        for slot in (artifact.get("taskContract") or {}).get("inputSlots") or []
        if slot.get("required", True) and slot.get("name") not in (binding_plan.get("bindings") or {})
    ]
    if missing:
        return {"success": False, "error": f"missing bindings: {missing}", "bindingPlan": binding_plan}

    orch = SimulationOrchestrator(_artifact_to_simulation_config(artifact, message))
    await orch._register_tools()
    try:
        tool_outputs: dict[str, Any] = {}
        records_offset = len(orch._collect_call_records())
        for step in path.get("steps") or []:
            tool_name = step.get("toolName")
            kwargs = _resolve_step_arguments(step, binding_plan, tool_outputs)
            result = await orch._tools.execute(tool_name, **kwargs)
            if result.error:
                return {
                    "success": False,
                    "error": result.error,
                    "bindingPlan": binding_plan,
                    "toolCalls": build_tool_call_record_events(orch._collect_call_records()[records_offset:]),
                }
            observation = _parse_json_or_text(result.output)
            if _observation_failed(observation):
                return {
                    "success": False,
                    "error": f"tool observation failed at {step.get('stepId')}: {_observation_error(observation)}",
                    "bindingPlan": binding_plan,
                    "toolCalls": build_tool_call_record_events(orch._collect_call_records()[records_offset:]),
                    "result": tool_outputs | {str(step.get("stepId")): observation},
                }
            tool_outputs[step.get("stepId")] = observation

        records = orch._collect_call_records()[records_offset:]
        orch._annotate_records(records, phase="golden_path_replay", purpose="replay_action")
        return {
            "success": True,
            "result": tool_outputs,
            "bindingPlan": binding_plan,
            "toolCalls": build_tool_call_record_events(records),
        }
    finally:
        await orch._mcp_conn.disconnect_all()


async def _build_binding_plan(
    artifact: dict[str, Any],
    path: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    slots = (artifact.get("taskContract") or {}).get("inputSlots") or []
    prompt = {
        "task": message,
        "taskContract": artifact.get("taskContract") or {},
        "goldenPath": {
            "pathId": path.get("pathId"),
            "steps": path.get("steps") or [],
            "applicability": path.get("applicability") or {},
        },
        "requiredOutput": {
            "useGoldenPath": "boolean",
            "reason": "string",
            "bindings": {slot.get("name"): "value" for slot in slots if slot.get("name")},
        },
    }
    try:
        llm = LLM(config.llm)
        resp = await llm.complete([
            {"role": "system", "content": "判断当前任务是否适合给定 GoldenPath，并抽取槽位。只输出 JSON。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ])
        parsed = _extract_json(resp.content or "")
        if parsed:
            parsed.setdefault("useGoldenPath", True)
            parsed.setdefault("bindings", {})
            return parsed
    except Exception as exc:
        logger.warning(f"BindingPlan LLM failed: {exc}")
    return {
        "useGoldenPath": True,
        "reason": "fallback binding: full task text bound to task slot",
        "bindings": {"task": message},
    }


def _resolve_step_arguments(
    step: dict[str, Any],
    binding_plan: dict[str, Any],
    tool_outputs: dict[str, Any],
) -> dict[str, Any]:
    bindings = binding_plan.get("bindings") or {}
    args = dict(step.get("argumentTemplate") or {})
    for key, spec in (step.get("inputMapping") or {}).items():
        source = spec.get("from")
        value = None
        if source == "slot":
            value = bindings.get(spec.get("name"))
        elif source == "step_output":
            value = tool_outputs.get(spec.get("stepId"))
            value = _json_path(value, spec.get("path") or "$")
        if value is None:
            continue
        if key in args and _is_control_argument(key):
            continue
        if key in args and not _binding_value_compatible(args[key], value):
            continue
        args[key] = value
    return {k: v for k, v in args.items() if v is not None}


def _is_control_argument(key: str) -> bool:
    return key in {"by", "tool_id", "include_references", "include_param_sources"}


def _binding_value_compatible(template: Any, value: Any) -> bool:
    if template is None:
        return True
    if isinstance(template, list):
        if not isinstance(value, list):
            return False
        template_tool_ids = _tool_ids_in_calculations(template)
        value_tool_ids = _tool_ids_in_calculations(value)
        return not template_tool_ids or template_tool_ids == value_tool_ids
    if isinstance(template, dict):
        return isinstance(value, dict)
    return isinstance(value, type(template))


def _tool_ids_in_calculations(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(row.get("tool_id"))
        for row in value
        if isinstance(row, dict) and row.get("tool_id")
    }


async def _run_slow_mode(artifact: dict[str, Any], message: str) -> dict[str, Any]:
    llm = LLM(config.llm)
    agent = MetaAppAgent(llm=llm)
    await agent.initialize_from_config(_artifact_to_meta_app_config(artifact), use_sim=False)
    events = []
    result = ""
    async for event in agent.run(message):
        events.append(event.to_dict())
        if event.type == "done":
            result = event.data.get("result", "")
    return {"success": True, "result": result, "events": events}


async def evaluate_with_verifier(
    artifact: dict[str, Any],
    task: str,
    run_result: dict[str, Any],
) -> dict[str, Any]:
    verifier = Agent(
        name="artifact_eval_verifier",
        llm=LLM(config.llm),
        system_prompt=(
            "你是元应用实验评价 Verifier。根据任务契约、用户任务和运行输出，"
            "判断业务语义是否满足。只输出 JSON："
            "{\"verdict\":\"passed|failed\",\"reason\":\"...\"}"
        ),
        max_steps=1,
    )
    payload = {
        "taskContract": artifact.get("taskContract") or {},
        "task": task,
        "runResult": run_result,
    }
    text = ""
    async for event in verifier.run(json.dumps(payload, ensure_ascii=False)):
        if event.type in ("think", "done"):
            text = event.data.get("thought") or event.data.get("result") or text
    parsed = _extract_json(text) or {}
    verdict = str(parsed.get("verdict") or "").lower()
    return {
        "schemaVersion": "verifier_result.v1",
        "verifierRole": "eval_verifier",
        "target": "experiment_trial",
        "verdict": "passed" if verdict == "passed" else "failed",
        "reason": parsed.get("reason") or text[:500],
        "model": config.llm.model,
    }


def _artifact_to_simulation_config(artifact: dict[str, Any], message: str) -> dict[str, Any]:
    app = artifact.get("app") or {}
    services = [_binding_to_service_meta(b) for b in (artifact.get("runtime") or {}).get("serviceBindings") or []]
    return {
        "appName": app.get("name") or "元应用",
        "domain": app.get("domain") or "generic",
        "scenarioDescription": message,
        "servicesMeta": services,
        "serviceIds": [s.get("id") for s in services],
        "strategy": {"sandbox": "none", "verification": "multi_agent"},
    }


def _artifact_to_meta_app_config(artifact: dict[str, Any]) -> dict[str, Any]:
    app = artifact.get("app") or {}
    task = artifact.get("taskContract") or {}
    services = []
    for binding in (artifact.get("runtime") or {}).get("serviceBindings") or []:
        services.append({
            "id": binding.get("serviceId"),
            "name": binding.get("serviceName"),
            "apiList": [{
                "url": binding.get("endpoint"),
                "method": "sse",
                "des": binding.get("serviceName") or binding.get("serviceId"),
                "tools": [
                    {
                        "id": t.get("toolName"),
                        "name": t.get("toolName"),
                        "description": t.get("description") or "",
                    }
                    for t in binding.get("tools") or []
                ],
            }],
        })
    return {
        "info": {
            "name": app.get("name"),
            "des": task.get("goal") or app.get("description"),
            "inputName": ", ".join(s.get("name") for s in task.get("inputSlots") or [] if s.get("name")) or "输入",
            "outputName": ", ".join(s.get("name") for s in task.get("outputSlots") or [] if s.get("name")) or "输出",
            "outputVisualization": False,
        },
        "services": services,
    }


def _binding_to_service_meta(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": binding.get("serviceId"),
        "name": binding.get("serviceName"),
        "isFake": binding.get("source") != "real_mcp",
        "mcpMethod": binding.get("transport") or "sse",
        "mcpUrl": binding.get("endpoint") or "",
        "tools": [
            {
                "id": t.get("toolName"),
                "name": t.get("toolName"),
                "description": t.get("description") or "",
                "inputSchema": t.get("inputSchema") or {},
            }
            for t in binding.get("tools") or []
        ],
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _parse_json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text


def _observation_failed(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip()
        return text.lower().startswith("error")
    if not isinstance(value, dict):
        return False
    if value.get("success") is False:
        return True
    if value.get("all_success") is False:
        return True
    if value.get("error") and value.get("success") is not True and value.get("all_success") is not True:
        return True
    rows = value.get("results")
    if isinstance(rows, list):
        return any(isinstance(row, dict) and row.get("success") is False for row in rows)
    return False


def _observation_error(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("error"):
            return str(value.get("error"))
        rows = value.get("results")
        if isinstance(rows, list):
            failed = next((row for row in rows if isinstance(row, dict) and row.get("success") is False), None)
            if failed:
                return str(failed.get("error") or failed)
    return str(value)[:300]


def _json_path(value: Any, path: str) -> Any:
    if path == "$" or not path:
        return value
    cur = value
    for part in path.strip("$.").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


__all__ = ["run_artifact", "evaluate_with_verifier"]
