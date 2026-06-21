"""Meta-app build compiler.

This module intentionally replaces the old ArtifactSpec/solidification-gate
compiler. It has one job: compile construction records into three separate
objects with clean semantics:

- ServiceSelectionReport: build-time explanation, never part of the artifact.
- AcceptedTrajectory: verifier-accepted action spine, never part of the artifact.
- MetaAppArtifact: the minimal runnable meta-app product.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


ARTIFACT_SCHEMA = "meta_app_artifact.v1"
ACCEPTED_TRAJECTORY_SCHEMA = "accepted_trajectory.v1"
SERVICE_SELECTION_SCHEMA = "service_selection_report.v1"

DEFAULT_FALLBACK_POLICY = {
    "onApplicabilityMismatch": "run_slow_mode",
    "onBindingFailure": "run_slow_mode",
    "onToolFailure": "run_slow_mode",
    "onAssertionFailure": "run_slow_mode",
}


@dataclass
class MetaAppArtifact:
    schemaVersion: str
    artifactId: str
    app: dict[str, Any]
    taskContract: dict[str, Any]
    runtime: dict[str, Any]
    goldenPaths: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompiledBuild:
    buildId: str
    serviceSelection: dict[str, Any]
    acceptedTrajectory: dict[str, Any]
    artifact: dict[str, Any]
    frontendState: dict[str, Any]


def stable_hash(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compile_build(trace: dict[str, Any]) -> CompiledBuild:
    """Compile all new build objects from a single BuildTrace dict."""
    build_id = _build_id(trace)
    service_selection = build_service_selection_report(trace)
    accepted = build_accepted_trajectory(trace)
    artifact = build_meta_app_artifact(trace, accepted)
    frontend = build_frontend_state(trace, service_selection, accepted, artifact)
    return CompiledBuild(
        buildId=build_id,
        serviceSelection=service_selection,
        acceptedTrajectory=accepted,
        artifact=artifact,
        frontendState=frontend,
    )


def compile_artifact_spec(trace: dict[str, Any], pipeline_result: Any | None = None) -> MetaAppArtifact:
    """Compatibility name for callers; returns the new minimal artifact object.

    `pipeline_result` is ignored by design. Evidence/diagnostics are build-time
    data and do not affect the final runnable artifact.
    """
    return MetaAppArtifact(**compile_build(trace).artifact)


def build_service_selection_report(trace: dict[str, Any]) -> dict[str, Any]:
    for ev in trace.get("events", []):
        if ev.get("type") == "service_selection":
            data = ev.get("data") or {}
            if isinstance(data, dict):
                return data

    cfg = _config(trace)
    services = list(cfg.get("servicesMeta") or [])
    requested = {str(x) for x in cfg.get("serviceIds") or [] if x}
    selected = []
    rejected = []
    for svc in services:
        sid = str(svc.get("id") or "")
        row = {
            "serviceId": sid,
            "serviceName": svc.get("name") or sid,
            "reason": "fallback selection from provided serviceIds" if requested else "fallback selection from provided catalog",
            "matchedCapabilities": _tool_names_from_meta(svc),
        }
        if not requested or sid in requested:
            selected.append(row)
        else:
            rejected.append({
                "serviceId": sid,
                "serviceName": svc.get("name") or sid,
                "reason": "not listed in requested serviceIds",
            })
    return {
        "schemaVersion": SERVICE_SELECTION_SCHEMA,
        "selectionId": _short_id("sel", stable_hash(selected)),
        "strategy": "provided_catalog_fallback",
        "selectedServices": selected,
        "rejectedServices": rejected,
        "missingCapabilities": [],
        "rationale": "LLM service selection was not available; used provided service ids/catalog.",
        "model": None,
        "createdAt": _now(),
    }


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
    service_bindings = _runtime_service_bindings(trace, accepted)
    task_contract = _task_contract(app, scenario, accepted)
    golden_paths = _golden_paths(accepted, task_contract)
    artifact = {
        "schemaVersion": ARTIFACT_SCHEMA,
        "artifactId": _short_id("app", stable_hash({"app": app, "task": task_contract, "services": service_bindings})),
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
    return artifact


def build_frontend_state(
    trace: dict[str, Any],
    service_selection: dict[str, Any],
    accepted: dict[str, Any],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    events = trace.get("events") or []
    tool_calls = _tool_records(trace)
    complete = next((e.get("data") for e in reversed(events) if e.get("type") == "complete"), {})
    return {
        "schemaVersion": "simulation_frontend_state.v1",
        "buildId": _build_id(trace),
        "app": artifact.get("app") or {},
        "taskContract": artifact.get("taskContract") or {},
        "serviceSelection": service_selection,
        "acceptedTrajectorySummary": {
            "trajectoryId": accepted.get("trajectoryId"),
            "status": accepted.get("status"),
            "acceptedIteration": accepted.get("acceptedIteration"),
            "actionCount": len(accepted.get("actionSequence") or []),
            "bindingGaps": accepted.get("bindingGaps") or [],
        },
        "artifactSummary": {
            "artifactId": artifact.get("artifactId"),
            "schemaVersion": artifact.get("schemaVersion"),
            "runtimeMode": (artifact.get("runtime") or {}).get("mode"),
            "goldenPathCount": len(artifact.get("goldenPaths") or []),
        },
        "callChain": [
            f"{c.get('service_name') or c.get('service_id')} · {c.get('tool_name')}"
            for c in tool_calls if _is_business_action(c)
        ],
        "events": {
            "count": len(events),
            "toolCallCount": len(tool_calls),
            "verifierResults": [
                e.get("data") for e in events if e.get("type") == "verifier_result"
            ],
        },
        "completion": complete or {},
        "artifact": artifact,
    }


def attach_artifact_hash_to_accepted(
    accepted: dict[str, Any],
    *,
    artifact_id: str,
    artifact_hash: str,
) -> dict[str, Any]:
    updated = json.loads(json.dumps(accepted, ensure_ascii=False))
    updated["generatedArtifact"] = {
        "artifactId": artifact_id,
        "artifactHash": artifact_hash,
        "recordedAt": _now(),
    }
    return updated


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
    slot_names = []
    for action in accepted.get("actionSequence") or []:
        for slot in action.get("inputSlots") or []:
            name = slot.get("name")
            if name and name not in slot_names:
                slot_names.append(name)
    input_slots = [
        {"name": name, "type": "unknown", "required": True}
        for name in slot_names
    ]
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


def _runtime_service_bindings(trace: dict[str, Any], accepted: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = _config(trace)
    services = {str(s.get("id") or ""): s for s in cfg.get("servicesMeta") or []}
    action_services = {str(a.get("serviceId") or "") for a in accepted.get("actionSequence") or []}
    selected = set()
    report = build_service_selection_report(trace)
    for row in report.get("selectedServices") or []:
        if row.get("serviceId"):
            selected.add(str(row.get("serviceId")))
    if action_services:
        selected.update(action_services)
    if not selected:
        selected = set(services)

    calls_by_service: dict[str, list[dict[str, Any]]] = {}
    for call in _tool_records(trace):
        sid = str(call.get("service_id") or "")
        calls_by_service.setdefault(sid, []).append(call)

    bindings = []
    for sid in sorted(s for s in selected if s):
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
        "goal": cfg.get("scenarioSummary") or cfg.get("scenarioDescription") or "",
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
    value = svc.get("isFake", svc.get("is_fake", False))
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes")


def _transport(svc: dict[str, Any]) -> str:
    method = str(svc.get("mcpMethod") or svc.get("method") or "sse").lower()
    if method in ("streamable-http", "streamable_http", "http"):
        return "streamable_http"
    return method


def _tool_names_from_meta(svc: dict[str, Any]) -> list[str]:
    return [
        str(t.get("name") or t.get("id"))
        for t in svc.get("tools") or []
        if t and (t.get("name") or t.get("id"))
    ]


__all__ = [
    "ARTIFACT_SCHEMA",
    "ACCEPTED_TRAJECTORY_SCHEMA",
    "SERVICE_SELECTION_SCHEMA",
    "MetaAppArtifact",
    "CompiledBuild",
    "compile_build",
    "compile_artifact_spec",
    "build_service_selection_report",
    "build_accepted_trajectory",
    "build_meta_app_artifact",
    "build_frontend_state",
    "attach_artifact_hash_to_accepted",
    "stable_hash",
]
