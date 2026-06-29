"""把构建事实编译为验收轨迹和可运行元应用配置。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any


ARTIFACT_SCHEMA = "meta_app_artifact.v1"
ACCEPTED_TRAJECTORY_SCHEMA = "accepted_trajectory.v1"

DEFAULT_FALLBACK_POLICY = {
    "onApplicabilityMismatch": "run_slow_mode",
    "onBindingFailure": "run_slow_mode",
    "onToolFailure": "run_slow_mode",
    "onAssertionFailure": "run_slow_mode",
}


def stable_hash(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_accepted_trajectory(trace: dict[str, Any]) -> dict[str, Any]:
    build_id = _build_id(trace)
    verifier = _final_passed_verifier(trace)
    if not verifier:
        return {
            "schemaVersion": ACCEPTED_TRAJECTORY_SCHEMA,
            "trajectoryId": "",
            "buildId": build_id,
            "status": "missing",
            "acceptedIteration": None,
            "verifier": None,
            "actionSequence": [],
            "bindingGaps": ["missing_accepted_verifier_iteration"],
            "generatedArtifact": {},
        }

    iteration = verifier.get("iteration")
    calls = [
        c for c in _tool_records(trace)
        if _is_business_action(c) and (iteration is None or c.get("iteration") == iteration)
    ]
    if not calls:
        calls = [c for c in _tool_records(trace) if _is_business_action(c)]

    actions = []
    binding_gaps = []
    previous_step_ids: list[str] = []
    for idx, call in enumerate(calls, start=1):
        step_id = f"s{idx}"
        arguments = call.get("arguments") or {}
        input_slots = [
            {
                "name": str(k),
                "source": "runtime_input",
                "type": _json_type(v),
            }
            for k, v in arguments.items()
            if k != "action"
        ]
        if not input_slots and arguments:
            binding_gaps.append(f"{step_id}: no_runtime_slots_inferred")
        actions.append({
            "stepId": step_id,
            "actionId": call.get("action_id") or f"a{idx}",
            "callId": call.get("call_id"),
            "serviceId": call.get("service_id"),
            "serviceName": call.get("service_name"),
            "toolName": call.get("tool_name"),
            "source": call.get("source") or call.get("channel"),
            "transport": call.get("transport"),
            "arguments": arguments,
            "argumentTemplate": arguments,
            "observation": {
                "success": bool(call.get("success")),
                "semanticSuccess": _semantic_success(call),
                "result": call.get("result"),
                "error": call.get("error"),
                "latencyMs": call.get("latency_ms"),
            },
            "inputSlots": input_slots,
            "dependsOn": list(previous_step_ids[-1:]),
        })
        previous_step_ids.append(step_id)

    status = "accepted" if actions else "missing"
    return {
        "schemaVersion": ACCEPTED_TRAJECTORY_SCHEMA,
        "trajectoryId": _short_id("traj", stable_hash({"build": build_id, "actions": actions})),
        "buildId": build_id,
        "status": status,
        "acceptedIteration": iteration,
        "verifier": {
            "role": verifier.get("verifierRole") or "build_verifier",
            "status": verifier.get("status"),
            "summary": verifier.get("summary") or verifier.get("reason") or "",
            "eventRef": f"verifier_result#iter{iteration}" if iteration else "verifier_result",
        },
        "actionSequence": actions,
        "bindingGaps": binding_gaps,
        "generatedArtifact": {},
    }


def build_meta_app_artifact(trace: dict[str, Any], accepted: dict[str, Any]) -> dict[str, Any]:
    cfg = _config(trace)
    scenario = _scenario(trace)
    app = {
        "name": trace.get("app_name") or cfg.get("appName") or "元应用",
        "domain": trace.get("domain") or cfg.get("domain") or scenario.get("domain") or "generic",
        "description": scenario.get("description") or cfg.get("scenarioDescription") or "",
    }
    service_bindings = _runtime_service_bindings(trace)
    task_contract = _task_contract(app, scenario, accepted)
    golden_paths = _golden_paths(accepted, task_contract)
    artifact = {
        "schemaVersion": ARTIFACT_SCHEMA,
        "app": app,
        "taskContract": task_contract,
        "runtime": {
            "mode": "agent_with_optional_golden_path" if golden_paths else "agent_only",
            "serviceBindings": service_bindings,
            "fallbackPolicy": DEFAULT_FALLBACK_POLICY,
            "agent": {
                "style": "react_slow_mode",
                "goldenPathDecision": "agent_internal",
            },
        },
        "goldenPaths": golden_paths,
    }
    artifact_id = _short_id("app", stable_hash(artifact))
    return {"schemaVersion": ARTIFACT_SCHEMA, "artifactId": artifact_id, **artifact}


def _golden_paths(accepted: dict[str, Any], task_contract: dict[str, Any]) -> list[dict[str, Any]]:
    actions = accepted.get("actionSequence") or []
    if accepted.get("status") != "accepted" or not actions:
        return []

    steps = []
    assertions = []
    for idx, action in enumerate(actions, start=1):
        step_id = action.get("stepId") or f"s{idx}"
        input_mapping = {}
        for slot in action.get("inputSlots") or []:
            name = slot.get("name")
            if name:
                input_mapping[name] = {"from": "slot", "name": name}
        steps.append({
            "stepId": step_id,
            "serviceId": action.get("serviceId"),
            "toolName": action.get("toolName"),
            "argumentTemplate": action.get("argumentTemplate") or action.get("arguments") or {},
            "inputMapping": input_mapping,
            "outputSlots": [{"name": f"{step_id}_output", "path": "$"}],
            "dependsOn": action.get("dependsOn") or [],
        })
        assertions.append({
            "assertionId": f"{step_id}_call_success",
            "level": "L1",
            "type": "tool_call_success",
            "target": {"stepId": step_id},
            "expected": {"success": True},
            "checkMode": "rule",
        })
        for name in input_mapping:
            assertions.append({
                "assertionId": f"{step_id}_{name}_bound",
                "level": "L2",
                "type": "input_slot_bound",
                "target": {"stepId": step_id, "slot": name},
                "expected": {"bound": True},
                "checkMode": "rule",
            })

    return [{
        "pathId": _short_id("gp", stable_hash(steps)),
        "primary": True,
        "status": "active",
        "sourceTrajectoryId": accepted.get("trajectoryId"),
        "applicability": {
            "requiredServices": sorted({s.get("serviceId") for s in steps if s.get("serviceId")}),
            "requiredInputSlots": task_contract.get("inputSlots") or [],
            "agentSemanticDecision": True,
        },
        "steps": steps,
        "assertions": assertions,
        "fallbackPolicy": DEFAULT_FALLBACK_POLICY,
    }]


def _task_contract(app: dict[str, Any], scenario: dict[str, Any], accepted: dict[str, Any]) -> dict[str, Any]:
    slot_names = set()
    input_slots = []
    for action in accepted.get("actionSequence") or []:
        for slot in action.get("inputSlots") or []:
            name = slot.get("name")
            if name and name not in slot_names:
                slot_names.add(name)
                input_slots.append({
                    "name": name,
                    "type": slot.get("type") or "unknown",
                    "required": True,
                })
    if not input_slots:
        input_slots = [{"name": "task", "type": "string", "required": True}]
    return {
        "goal": scenario.get("goal") or app.get("description") or app.get("name") or "",
        "domain": app.get("domain") or scenario.get("domain") or "generic",
        "inputSlots": input_slots,
        "outputSlots": [{"name": "result", "type": "object", "required": True}],
        "constraints": list(scenario.get("constraints") or []),
        "successCriteria": list(scenario.get("acceptanceCriteria") or scenario.get("acceptance") or []),
    }


def _runtime_service_bindings(trace: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = _config(trace)
    services = {str(s.get("id") or ""): s for s in cfg.get("servicesMeta") or []}

    calls_by_service: dict[str, list[dict[str, Any]]] = {}
    for call in _tool_records(trace):
        sid = str(call.get("service_id") or "")
        calls_by_service.setdefault(sid, []).append(call)

    bindings = []
    for sid in sorted(s for s in services if s):
        svc = services.get(sid, {})
        calls = calls_by_service.get(sid, [])
        tools = []
        seen = set()
        for call in calls:
            name = call.get("tool_name")
            if not name or name in seen:
                continue
            seen.add(name)
            tools.append({
                "toolName": name,
                "description": "",
                "inputSchema": {},
            })
        for t in svc.get("tools") or []:
            name = t.get("name") or t.get("id")
            if name and name not in seen:
                seen.add(name)
                tools.append({
                    "toolName": name,
                    "description": t.get("description") or t.get("des") or "",
                    "inputSchema": t.get("inputSchema") or {},
                })
        bindings.append({
            "serviceId": sid,
            "serviceName": svc.get("name") or sid,
            "isFake": _is_fake(svc),
            "source": "demo_fake_mcp" if _is_fake(svc) else "real_mcp",
            "transport": _transport(svc),
            "endpoint": svc.get("mcpUrl") or svc.get("url") or "",
            "schemaHash": stable_hash({"tools": tools})[:16],
            "tools": tools,
        })
    return bindings


def _final_passed_verifier(trace: dict[str, Any]) -> dict[str, Any] | None:
    if trace.get("success") is not True:
        return None
    results = [
        e.get("data") for e in trace.get("events", [])
        if e.get("type") == "verifier_result" and isinstance(e.get("data"), dict)
    ]
    final_iteration = trace.get("iterations")
    for row in reversed(results):
        if final_iteration is not None and row.get("iteration") != final_iteration:
            continue
        status = str(row.get("status") or row.get("verdict") or "").lower()
        if status in ("passed", "pass"):
            return row
    return None


def _tool_records(trace: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        e.get("data") for e in trace.get("events", [])
        if e.get("type") == "tool_call_record" and isinstance(e.get("data"), dict)
    ]
    return sorted(rows, key=lambda x: x.get("timestamp") or 0)


def _is_business_action(call: dict[str, Any]) -> bool:
    if call.get("purpose") == "service_probe":
        return False
    if (call.get("arguments") or {}).get("action") == "health_check":
        return False
    if call.get("service_id") == "internal":
        return False
    return bool(call.get("tool_name")) and bool(call.get("success")) and _semantic_success(call)


def _semantic_success(call: dict[str, Any]) -> bool:
    """True when the tool transport succeeded and the observation is not a domain error."""
    if call.get("success") is False:
        return False
    if call.get("error"):
        return False
    result = call.get("result")
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return True
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            return not text.lower().startswith("error")
    if isinstance(result, dict):
        if result.get("success") is False:
            return False
        if result.get("all_success") is False:
            return False
        if result.get("error") and result.get("success") is not True and result.get("all_success") is not True:
            return False
        rows = result.get("results")
        if isinstance(rows, list):
            return all(not isinstance(row, dict) or row.get("success") is not False for row in rows)
    return True


def _scenario(trace: dict[str, Any]) -> dict[str, Any]:
    for ev in trace.get("events", []):
        if ev.get("type") == "scenario_parsed" and isinstance(ev.get("data"), dict):
            return ev["data"]
    cfg = _config(trace)
    parsed = cfg.get("scenarioParsed")
    if isinstance(parsed, dict) and parsed:
        return parsed
    return {
        "goal": cfg.get("scenarioDescription") or "",
        "description": cfg.get("scenarioDescription") or "",
        "constraints": [],
        "acceptanceCriteria": [],
        "domain": cfg.get("domain") or trace.get("domain") or "generic",
    }


def _config(trace: dict[str, Any]) -> dict[str, Any]:
    meta = trace.get("metadata") or {}
    return meta.get("config_snapshot") or trace.get("config") or {}


def _build_id(trace: dict[str, Any]) -> str:
    return str(trace.get("build_id") or trace.get("session_id") or trace.get("id") or "")


def _short_id(prefix: str, seed: str) -> str:
    text = re.sub(r"[^a-fA-F0-9]", "", str(seed))
    if len(text) < 12:
        text = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
    return f"{prefix}-{text[:16].lower()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "string"


def _is_fake(svc: dict[str, Any]) -> bool:
    return svc.get("isFake") is True


def _transport(svc: dict[str, Any]) -> str:
    method = str(svc.get("mcpMethod") or svc.get("method") or "sse").lower()
    if method in ("streamable-http", "streamable_http", "http"):
        return "streamable_http"
    return method


__all__ = [
    "ARTIFACT_SCHEMA",
    "ACCEPTED_TRAJECTORY_SCHEMA",
    "build_accepted_trajectory",
    "build_meta_app_artifact",
    "stable_hash",
]
