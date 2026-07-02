"""Runtime for locally running a compiled MetaAppArtifact."""

from __future__ import annotations

import json
import time
from typing import Any, TYPE_CHECKING

from loguru import logger

from micro_agent.core.agent import Agent
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.simulation.service_tool_session import ServiceToolSession
from micro_agent.simulation.trace_records import annotate_records, build_tool_call_record_events

if TYPE_CHECKING:
    from micro_agent.data_file import FileRegistry


async def run_artifact(
    artifact: dict[str, Any],
    message: str,
    *,
    prefer_golden_path: bool = True,
    file_registry: FileRegistry | None = None,
    input_file_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run a meta-app artifact once.

    GoldenPath is attempted first when available. It is an internal runtime
    asset, not an external router: an LLM decides applicability and emits a
    structured BindingPlan. If the fast path fails, slow-mode Agent execution
    is used.
    """
    started = time.time()
    input_file_ids = input_file_ids or []
    if file_registry and input_file_ids:
        from micro_agent.tool.data_file import data_file_context
        message = f"{message.strip()}\n\n{data_file_context(file_registry, input_file_ids)}".strip()
    fast_result = None
    if prefer_golden_path and artifact.get("goldenPaths") and not input_file_ids:
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

    slow_result = await _run_slow_mode(artifact, message, file_registry)
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
        "error": slow_result.get("error"),
        "events": slow_result.get("events") or [],
        "toolCalls": slow_result.get("toolCalls") or [],
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

    runtime_config = _artifact_to_simulation_config(artifact, message)
    required_services = {step.get("serviceId") for step in path.get("steps") or []}
    runtime_config["servicesMeta"] = [
        service for service in runtime_config["servicesMeta"]
        if service.get("id") in required_services
    ]
    try:
        async with ServiceToolSession(runtime_config["servicesMeta"]) as session:
            await session.connect()
            tool_outputs: dict[str, Any] = {}
            records_offset = len(session.records())
            for step in path.get("steps") or []:
                tool_name = step.get("toolName")
                kwargs = _resolve_step_arguments(step, binding_plan, tool_outputs)
                result = await session.tools.execute(tool_name, **kwargs)
                if result.error:
                    return {
                        "success": False,
                        "error": result.error,
                        "bindingPlan": binding_plan,
                        "toolCalls": build_tool_call_record_events(session.records()[records_offset:]),
                    }
                observation = _parse_json_or_text(result.output)
                if _observation_failed(observation):
                    return {
                        "success": False,
                        "error": f"tool observation failed at {step.get('stepId')}: {_observation_error(observation)}",
                        "bindingPlan": binding_plan,
                        "toolCalls": build_tool_call_record_events(session.records()[records_offset:]),
                        "result": tool_outputs | {str(step.get("stepId")): observation},
                    }
                tool_outputs[step.get("stepId")] = observation

            records = session.records()[records_offset:]
            annotate_records(records, "golden_path_replay", "replay_action")
            return {
                "success": True,
                "result": tool_outputs,
                "bindingPlan": binding_plan,
                "toolCalls": build_tool_call_record_events(records),
            }
    except Exception as exc:
        return {"success": False, "error": str(exc), "bindingPlan": binding_plan}


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
        return {"useGoldenPath": False, "reason": str(exc), "bindings": {}}
    return {"useGoldenPath": False, "reason": "invalid_binding_plan", "bindings": {}}


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
        if value is not None:
            args[key] = value
    return {k: v for k, v in args.items() if v is not None}


async def _run_slow_mode(
    artifact: dict[str, Any],
    message: str,
    file_registry: FileRegistry | None = None,
) -> dict[str, Any]:
    services = _artifact_to_simulation_config(artifact, message)["servicesMeta"]
    task_contract = artifact.get("taskContract") or {}
    events = []
    result = ""
    error = ""
    completed = False
    from micro_agent.tool.data_file import data_file_tools

    async with ServiceToolSession(
        services,
        local_tools=data_file_tools(file_registry) if file_registry else None,
    ) as session:
        await session.connect()
        agent = Agent(
            name="artifact_slow_mode",
            llm=LLM(config.llm),
            tools=session.tools,
            system_prompt=(
                "根据任务契约调用已绑定服务完成用户任务，完成后调用 terminate。\n"
                f"任务契约: {json.dumps(task_contract, ensure_ascii=False)}"
            ),
            max_steps=20,
        )
        async for event in agent.run(message):
            row = event.to_dict()
            if event.type == "tool_result" and event.data.get("tool") in {"inspect_data_file", "read_data_file"}:
                row["data"]["result"] = "数据文件读取结果仅供当前 Agent 使用，运行记录已省略正文。"
            events.append(row)
            if event.type == "done":
                result = event.data.get("result", "")
                completed = event.data.get("reason") not in {"max_steps", "cancelled"}
            elif event.type == "error":
                error = str(event.data.get("error") or "agent_error")
        records = session.records()
        annotate_records(records, "slow_mode", "react_action")
        return {
            "success": completed and bool(result) and not error,
            "result": result,
            "error": error or None,
            "events": events,
            "toolCalls": build_tool_call_record_events(records),
        }


async def evaluate_with_verifier(
    artifact: dict[str, Any],
    task: str,
    run_result: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "taskContract": artifact.get("taskContract") or {},
        "task": task,
        "runResult": run_result,
    }
    response = await LLM(config.llm).complete([
        {
            "role": "system",
            "content": (
                "根据任务契约、用户任务和运行输出判断业务语义是否满足。"
                "只输出 JSON：{\"verdict\":\"passed|failed\",\"reason\":\"...\"}"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ])
    text = response.content or ""
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
    }


def _binding_to_service_meta(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": binding.get("serviceId"),
        "name": binding.get("serviceName"),
        "isFake": binding.get("isFake") is True,
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
