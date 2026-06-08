"""ArtifactSpec v0 Compiler — trace → 结构化元应用产物

独立模块，从已持久化的 v1.0.0 TraceRecord JSON 编译 ArtifactSpec。
零侵入 orchestrator，只读 trace dict 和可选的 PipelineResult。

用法:
    from micro_agent.simulation.artifact_compiler import compile_artifact_spec
    spec = compile_artifact_spec(trace_dict)          # 仅 trace
    spec = compile_artifact_spec(trace_dict, pipeline_result)  # trace + evidence
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Dataclass hierarchy — mirrors artifact_spec_schema.json 1:1
# ---------------------------------------------------------------------------


@dataclass
class MetaAppInfo:
    appName: str
    domain: str
    mode: str = "production"
    appId: Optional[str] = None


@dataclass
class ScenarioInfo:
    title: str
    domain: str
    sourceDescription: str
    scenarioId: Optional[str] = None
    description: Optional[str] = None
    parsedIntent: Optional[dict] = None
    involvedServices: list[dict] = field(default_factory=list)
    evidenceRef: dict = field(default_factory=dict)


@dataclass
class ExceptionInfo:
    exceptionType: str
    message: str
    recoverable: bool


@dataclass
class StateNode:
    stateId: str
    state: str
    enteredAt: str
    iteration: int = 0
    exitedAt: Optional[str] = None
    durationMs: Optional[float] = None
    metadata: dict = field(default_factory=dict)
    exception: Optional[dict] = None
    evidenceRefs: list[str] = field(default_factory=list)


@dataclass
class BoundToolCall:
    callId: str
    toolName: str = ""
    serviceId: str = ""
    success: bool = True
    latencyMs: Optional[float] = None
    resultHash: Optional[str] = None


@dataclass
class Transition:
    transitionId: str
    fromState: str
    toState: str
    trigger: str
    iteration: int = 0
    occurredAt: Optional[str] = None
    boundToolCalls: list[dict] = field(default_factory=list)
    evidenceRefs: list[str] = field(default_factory=list)


@dataclass
class IterationPlanner:
    selectedTools: list[str]
    iteration: int = 1
    candidateTools: list[str] = field(default_factory=list)
    executionPath: list[str] = field(default_factory=list)
    reasonSummary: Optional[str] = None
    dispatchSteps: list[dict] = field(default_factory=list)
    totalExpectedCalls: int = 0
    evidenceRef: str = ""


@dataclass
class IterationVerifier:
    status: str = "PASSED"
    iteration: int = 1
    summary: Optional[str] = None
    checks: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    issueCount: int = 0
    evidenceRef: Optional[str] = None


@dataclass
class IterationToolCall:
    callId: str
    toolName: str
    serviceId: str
    channel: str
    success: bool
    serviceName: Optional[str] = None
    transport: Optional[str] = None
    arguments: dict = field(default_factory=dict)
    resultPreview: Optional[str] = None
    resultHash: Optional[str] = None
    error: Optional[str] = None
    latencyMs: Optional[float] = None
    timestamp: Optional[float] = None


@dataclass
class IterationException:
    hasToolError: bool = False
    hasVerifierFailure: bool = False
    failedToolCalls: list[str] = field(default_factory=list)
    verifierIssues: list[str] = field(default_factory=list)


@dataclass
class IterationSnapshot:
    iterationNumber: int
    states: list[str]
    planner: dict
    verifier: Optional[dict]
    toolCalls: list[dict]
    exception: Optional[dict] = None


@dataclass
class StateMachineTrace:
    states: list[dict]
    transitions: list[dict]
    iterations: list[dict]
    totalIterations: int = 0
    finalStatus: str = "UNKNOWN"
    elapsedMs: int = 0
    strategy: dict = field(default_factory=dict)


@dataclass
class EvidenceSnapshot:
    evidenceId: Optional[str] = None
    evidenceFingerprint: Optional[str] = None
    checkerStatus: Optional[str] = None
    completeness: Optional[str] = None
    missingEvidenceCategories: list[str] = field(default_factory=list)
    evidenceRef: Optional[str] = None


@dataclass
class ToolCallProvenance:
    callId: str
    resultHash: Optional[str] = None
    toolName: str = ""
    serviceId: str = ""
    channel: str = ""
    timestamp: Optional[float] = None


@dataclass
class Provenance:
    sourceSessionId: str
    sourceTraceVersion: str
    artifactHash: str
    traceHash: str = ""
    configSnapshotHash: str = ""
    compilerVersion: Optional[str] = None
    createdAt: str = ""
    toolCallProvenance: list[dict] = field(default_factory=list)


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str
    remediation: str = ""


@dataclass
class ConditionResult:
    passed: bool
    def _asdict(self, extra: dict | None = None) -> dict:
        return asdict(self) | (extra or {})


@dataclass
class BoolCondition(ConditionResult):
    def _asdict(self, extra: dict | None = None) -> dict:
        return {"passed": self.passed} | (extra or {})


@dataclass
class SolidificationReport:
    solidifiable: bool
    conditions: dict = field(default_factory=dict)
    gates: list[dict] = field(default_factory=list)


@dataclass
class WriteBackExistingFields:
    name: str = ""
    subtitle: str = ""
    services: list[str] = field(default_factory=list)
    inputName: str = ""
    outputName: str = ""
    outputVisualization: bool = False
    submitButtonText: str = ""
    des: str = ""


@dataclass
class WriteBackNewFields:
    artifactSpecJson: str = ""
    sourceSessionId: str = ""
    traceHash: str = ""
    artifactHash: str = ""
    schemaVersion: str = ""


@dataclass
class WriteBackDraft:
    existingFields: dict = field(default_factory=dict)
    newFields: dict = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)


@dataclass
class ArtifactSpec:
    """ArtifactSpec v0 — 从仿真 trace 编译的元应用产物"""
    schemaVersion: str = "0.1.0"
    artifactId: str = ""
    sourceSessionId: str = ""
    createdAt: str = ""
    metaApp: dict = field(default_factory=dict)
    scenario: dict = field(default_factory=dict)
    stateMachineTrace: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    solidifiable: bool = False
    solidificationReport: dict = field(default_factory=dict)
    evidence: Optional[dict] = None
    writeBackDraft: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


def _ts_to_iso(ts: float | int | None) -> str:
    """Unix epoch → ISO 8601 string."""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return ""


def _sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _short_id(prefix: str, seed: str) -> str:
    """Generate deterministic id like {prefix}-{6chars}-{8chars}."""
    h = _sha256(seed)
    return f"{prefix}-{h[:6]}-{h[8:16]}"


def compile_artifact_spec(
    trace: dict,
    pipeline_result: Any | None = None,
    *,
    schema_version: str = "0.1.0",
    compiler_version: str | None = None,
) -> ArtifactSpec:
    """Compile ArtifactSpec v0 from a v1.0.0 trace dict.

    Args:
        trace: TraceRecord.to_dict() output
        pipeline_result: Optional trace_evidence.PipelineResult for evidence enrichment
        schema_version: ArtifactSpec schema version
        compiler_version: Compiler version string (commit hash or tag)

    Returns:
        ArtifactSpec dataclass — guaranteed to pass artifact_spec_schema.json validation

    Raises:
        ValueError: trace version is not v1.0.0
    """
    # ---- gate: only v1.0.0 traces ----
    meta = trace.get("metadata", {})
    runtime = meta.get("runtime", {})
    trace_ver = runtime.get("trace_version") or meta.get("trace_version", "")
    if trace_ver != "v1.0.0":
        raise ValueError(
            f"ArtifactSpec compiler requires v1.0.0 traces, got {trace_ver!r}"
        )

    session_id = trace.get("session_id", "")
    events: list[dict] = trace.get("events", [])

    # ---- metaApp ----
    meta_app = _build_meta_app(trace)

    # ---- scenario ----
    scenario = _build_scenario(trace, meta_app)

    # ---- stateMachineTrace ----
    smt = _build_state_machine(events, trace)

    # ---- evidence snapshot ----
    evidence = _build_evidence(pipeline_result)

    # Derive createdAt from trace.created_at for deterministic compilation
    created_at = _ts_to_iso(trace.get("created_at"))

    # ---- provenance ----
    config_snapshot = meta.get("config_snapshot", {})
    trace_hash = _sha256(json.dumps(trace, ensure_ascii=False, sort_keys=True))
    config_hash = _sha256(json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True))
    prov = _build_provenance(
        session_id=session_id,
        trace_version=trace_ver,
        trace_hash=trace_hash,
        config_hash=config_hash,
        events=events,
        compiler_version=compiler_version,
        created_at=created_at,
    )

    # ---- solidification report ----
    solidification = _build_solidification_report(trace, smt, evidence)

    # ---- writeBackDraft ----
    write_back = _build_write_back(trace, smt, scenario, meta_app, prov)

    # ---- assembly ----
    artifact_id = _short_id("art", f"{session_id}:{prov.traceHash}")

    spec = ArtifactSpec(
        schemaVersion=schema_version,
        artifactId=artifact_id,
        sourceSessionId=session_id,
        createdAt=created_at,
        metaApp=asdict(meta_app),
        scenario=asdict(scenario),
        stateMachineTrace=asdict(smt),
        provenance=asdict(prov),
        evidence=asdict(evidence) if evidence else None,
        solidifiable=solidification.solidifiable,
        solidificationReport=asdict(solidification),
        writeBackDraft=asdict(write_back) if write_back else None,
    )

    # ---- artifactHash (self-referential: compute over spec with artifactHash="") ----
    d = asdict(spec)
    d["provenance"]["artifactHash"] = ""
    art_hash = _sha256(json.dumps(d, ensure_ascii=False, sort_keys=True))
    spec.provenance["artifactHash"] = art_hash
    if spec.writeBackDraft:
        spec.writeBackDraft["newFields"]["artifactHash"] = art_hash
    spec.artifactId = _short_id("art", f"{session_id}:{art_hash}")

    return spec


# ---------------------------------------------------------------------------
# Sub-compilers
# ---------------------------------------------------------------------------


def _build_meta_app(trace: dict) -> MetaAppInfo:
    meta = trace.get("metadata", {})
    cfg = meta.get("config_snapshot", {})
    return MetaAppInfo(
        appName=trace.get("app_name", cfg.get("appName", "")),
        domain=trace.get("domain", cfg.get("domain", "")),
        mode=trace.get("mode", "production"),
        appId=cfg.get("appId") or None,
    )


def _build_scenario(trace: dict, meta_app: MetaAppInfo) -> ScenarioInfo:
    meta = trace.get("metadata", {})
    cfg = meta.get("config_snapshot", {})
    scenario_desc = cfg.get("scenarioDescription", "") or ""
    app_name = meta_app.appName

    # Build involved services from config_snapshot.servicesMeta
    involved: list[dict] = []
    for svc in cfg.get("servicesMeta", []):
        involved.append({
            "serviceId": str(svc.get("id", "")),
            "name": svc.get("name", ""),
            "description": svc.get("description") or None,
            "channel": svc.get("mcpMethod") or svc.get("channel") or None,
            "isFake": bool(svc.get("isFake", False)),
        })

    # scenarioId: deterministic from appName + domain + scenario desc
    scenario_id = _short_id("sc", f"{meta_app.appName}:{meta_app.domain}:{scenario_desc}")

    return ScenarioInfo(
        scenarioId=scenario_id,
        title=f"{app_name} — {scenario_desc[:80]}" if scenario_desc else app_name,
        description=scenario_desc or None,
        domain=meta_app.domain,
        involvedServices=involved,
        sourceDescription=scenario_desc,
        evidenceRef={
            "traceEventTypes": ["service", "planner_decision", "verifier_result"],
            "configSnapshotHash": _sha256(json.dumps(cfg, ensure_ascii=False, sort_keys=True)),
        },
    )


# ---- State Machine -------------------------------------------------------


def _build_state_machine(events: list[dict], trace: dict) -> StateMachineTrace:
    """Build states, transitions, and iteration snapshots from trace events."""

    # Collect events by type
    plan_decisions = [e for e in events if e.get("type") == "planner_decision"]
    verifier_results = [e for e in events if e.get("type") == "verifier_result"]
    tool_calls = [e for e in events if e.get("type") == "tool_call_record"]
    iter_events = [e for e in events if e.get("type") == "iteration"]
    phase_events = [e for e in events if e.get("type") == "phase"]
    service_events = [e for e in events if e.get("type") == "service"]
    complete_events = [e for e in events if e.get("type") == "complete"]

    first_ts = _first_ts(events)
    last_ts = _last_ts(events)

    # Determine final status
    complete = complete_events[0] if complete_events else None
    complete_data = complete.get("data", {}) if complete else {}
    if complete_data.get("cancelled"):
        final_status = "CANCELLED"
    elif complete_data.get("success") is True:
        final_status = "SUCCESS"
    elif complete_data.get("success") is False:
        final_status = "FAILED"
    else:
        final_status = "UNKNOWN"

    strategy = trace.get("strategy", {})

    # ---- Build states ----
    states: list[StateNode] = []
    transitions: list[Transition] = []
    prev_state_id: str | None = None

    def _add_state(state_id, state_name, entered_ts, iteration=0,
                   exited_ts=None, metadata=None, exception=None, evidence_refs=None):
        nonlocal prev_state_id
        e_ts = _ts_to_iso(entered_ts)
        x_ts = _ts_to_iso(exited_ts) if exited_ts else None
        dur = (float(exited_ts) - float(entered_ts)) * 1000 if exited_ts and entered_ts else None
        node = StateNode(
            stateId=state_id,
            state=state_name,
            enteredAt=e_ts,
            iteration=iteration,
            exitedAt=x_ts,
            durationMs=dur,
            metadata=metadata or {},
            exception=exception,
            evidenceRefs=evidence_refs or [],
        )
        states.append(node)
        if prev_state_id:
            trigger = _state_to_trigger(prev_state_id, state_name, states)
            transitions.append(Transition(
                transitionId=_short_id("trans", f"{prev_state_id}→{state_id}"),
                fromState=prev_state_id,
                toState=state_id,
                trigger=trigger,
                iteration=iteration,
                occurredAt=e_ts,
                evidenceRefs=[f"state/{prev_state_id}", f"state/{state_id}"],
            ))
        prev_state_id = state_id
        return state_id

    # Step 1: INITIALIZING → DISCOVERING
    trace_sid = trace.get('session_id', '')
    s_init = _add_state(
        _short_id("state", f"init:{trace_sid}"),
        "INITIALIZING", first_ts or 0,
        metadata={"phaseName": "init"},
    )

    # Service discovery timestamp
    svc_ts_list = [e.get("timestamp", 0) for e in service_events]
    disc_start = svc_ts_list[0] if svc_ts_list else (first_ts or 0)
    disc_end = svc_ts_list[-1] if svc_ts_list else disc_start

    discovered_ids = []
    for e in service_events:
        d = e.get("data", {})
        if d.get("status") == "online":
            discovered_ids.append(d.get("id", ""))

    s_disc = _add_state(
        _short_id("state", f"disc:{disc_start}"), "DISCOVERING", disc_start,
        metadata={"phaseName": "data", "servicesDiscovered": discovered_ids},
        exited_ts=disc_end,
    )

    # ---- Iteration-based states ----
    prev_state = s_disc
    prev_plan_ts = disc_end

    for iter_idx in range(len(plan_decisions)):
        plan = plan_decisions[iter_idx]
        plan_data = plan.get("data", {})
        iter_num = plan_data.get("iteration", iter_idx + 1)
        plan_ts = plan.get("timestamp", prev_plan_ts)

        # PLANNING
        s_plan = _add_state(
            _short_id("state", f"plan:{iter_num}:{plan_ts}"),
            "PLANNING", plan_ts, iteration=iter_num,
            metadata={
                "phaseName": "logic",
                "selectedTools": plan_data.get("selected_tools", []),
            },
        )

        # EXECUTING: bind tool_call_record events to this iteration via verifier evidence_refs.
        # Tool call timestamps can precede planner_decision because they reflect the actual
        # MCP call time (during data/discovery phase), not the planner_decision emit time.
        ver = verifier_results[iter_idx] if iter_idx < len(verifier_results) else None
        ver_data = ver.get("data", {}) if ver else {}
        ver_checks = ver_data.get("checks", []) if ver else []
        iter_call_ids: set[str] = set()
        for c in ver_checks:
            for ref in c.get("evidence_refs", []):
                if isinstance(ref, str):
                    iter_call_ids.add(ref)

        iter_tool_calls = [
            tc for tc in tool_calls
            if tc.get("data", {}).get("call_id", "") in iter_call_ids
        ]
        # Fallback: if verifier didn't provide evidence_refs (rare), use all tool calls
        # whose call_id hasn't been assigned to a prior iteration
        if not iter_tool_calls and not iter_call_ids:
            # Use all calls — single-iteration trace with no structured evidence refs
            iter_tool_calls = list(tool_calls)

        if iter_tool_calls:
            exec_start = min(tc.get("timestamp", plan_ts) for tc in iter_tool_calls)
            exec_end = max(tc.get("timestamp", plan_ts) for tc in iter_tool_calls)
            tool_success = sum(1 for tc in iter_tool_calls if tc.get("data", {}).get("success"))
            tool_fail = sum(1 for tc in iter_tool_calls if not tc.get("data", {}).get("success"))

            s_exec = _add_state(
                _short_id("state", f"exec:{iter_num}:{exec_start}"),
                "EXECUTING", exec_start, iteration=iter_num,
                exited_ts=exec_end,
                metadata={
                    "toolSuccessCount": tool_success,
                    "toolFailureCount": tool_fail,
                },
            )
            prev_state = s_exec
        else:
            exec_end = plan_ts
            prev_state = s_plan

        # VERIFYING
        if ver:
            ver_data = ver.get("data", {})
            ver_status = ver_data.get("status", "FAILED")
            ver_ts_val = ver.get("timestamp", exec_end + 0.001)

            s_ver = _add_state(
                _short_id("state", f"ver:{iter_num}:{ver_ts_val}"),
                "VERIFYING", ver_ts_val, iteration=iter_num,
                metadata={
                    "phaseName": "check",
                    "verifierStatus": ver_status,
                    "verifierIssues": ver_data.get("issues", []),
                },
            )

            # Decide PASSED or FAILED
            if ver_status == "PASSED":
                s_passed = _add_state(
                    _short_id("state", f"passed:{iter_num}:{ver_ts_val}"),
                    "PASSED", ver_ts_val, iteration=iter_num,
                )
                prev_state = s_passed
            else:
                issues = ver_data.get("issues", [])
                issue_msg = "; ".join(issues) if issues else "Verifier 语义裁决不通过"
                s_failed = _add_state(
                    _short_id("state", f"failed:{iter_num}:{ver_ts_val}"),
                    "FAILED", ver_ts_val, iteration=iter_num,
                    metadata={"verifierIssues": issues},
                    exception=asdict(ExceptionInfo(
                        exceptionType="SEMANTIC_FAILED" if issues else "ASSERTION_FAILED",
                        message=issue_msg,
                        recoverable=True,  # Planner can retry
                    )),
                )
                # RETRYING if more iterations to come
                if iter_idx + 1 < len(plan_decisions):
                    next_plan_ts_val = plan_decisions[iter_idx + 1].get("timestamp", ver_ts_val + 0.001)
                    s_retry = _add_state(
                        _short_id("state", f"retry:{iter_num}:{ver_ts_val}"),
                        "RETRYING", ver_ts_val, iteration=iter_num,
                        exited_ts=next_plan_ts_val,
                    )
                    prev_state = s_retry
                prev_plan_ts = ver_ts_val
        else:
            prev_plan_ts = exec_end

    # Final state
    max_iter = trace.get("iterations", 0)
    strategy_min = strategy.get("minIterations", 1)

    # Check for tool errors
    tool_errors = [tc for tc in tool_calls if not tc.get("data", {}).get("success")]
    max_reached = max_iter >= (strategy_min * 2) or max_iter >= 3  # heuristic for max exhausted

    if final_status == "CANCELLED":
        _add_state(
            _short_id("state", f"cancelled:{last_ts or 0}"),
            "CANCELLED", last_ts or 0,
        )
    elif final_status == "FAILED":
        # Determine if terminal or retry
        if max_reached:
            _add_state(
                _short_id("state", f"terminal:{last_ts or 0}"),
                "TERMINAL_FAILED", last_ts or 0,
                exception=asdict(ExceptionInfo(
                    exceptionType="MAX_ITERATIONS_EXHAUSTED",
                    message=f"已达最大迭代轮次 {max_iter}，构建未成功",
                    recoverable=False,
                )),
            )
        else:
            _add_state(
                _short_id("state", f"terminal:{last_ts or 0}"),
                "TERMINAL_FAILED", last_ts or 0,
                exception=asdict(ExceptionInfo(
                    exceptionType="INFRASTRUCTURE_ERROR",
                    message=complete_data.get("result", {}).get("error", "构建失败"),
                    recoverable=False,
                )),
            )
    elif final_status == "SUCCESS":
        _add_state(
            _short_id("state", f"completed:{last_ts or 0}"),
            "COMPLETED", last_ts or 0,
        )

    # ---- Build iteration snapshots ----
    iteration_snapshots: list[IterationSnapshot] = []
    assigned_call_ids: set[str] = set()
    for iter_idx in range(len(plan_decisions)):
        plan = plan_decisions[iter_idx]
        plan_data = plan.get("data", {})
        iter_num = plan_data.get("iteration", iter_idx + 1)
        plan_ts = plan.get("timestamp", 0)

        ver = verifier_results[iter_idx] if iter_idx < len(verifier_results) else None
        ver_data = ver.get("data", {}) if ver else {}
        ver_checks = ver_data.get("checks", []) if ver else []

        # Bind tool calls to iteration via verifier evidence_refs (same logic as state builder)
        iter_call_ids: set[str] = set()
        for c in ver_checks:
            for ref in c.get("evidence_refs", []):
                if isinstance(ref, str):
                    iter_call_ids.add(ref)

        # First pass: use evidence_refs
        iter_tc = [tc for tc in tool_calls if tc.get("data", {}).get("call_id", "") in iter_call_ids]
        # Second pass: assign any remaining unassigned tool calls to this iteration
        if not iter_call_ids:
            # If verifier gave no evidence refs, just grab all calls (single-iteration fallback)
            iter_tc = [tc for tc in tool_calls if tc.get("data", {}).get("call_id", "") not in assigned_call_ids]
        assigned_call_ids.update(tc.get("data", {}).get("call_id", "") for tc in iter_tc)

        # States belonging to this iteration
        iter_state_ids = [
            s.stateId for s in states if s.iteration == iter_num
        ]

        # Iteration planner snapshot
        dispatch_steps = plan_data.get("dispatch", {}).get("steps", [])
        planner_dict = asdict(IterationPlanner(
            iteration=iter_num,
            candidateTools=plan_data.get("candidate_tools", []),
            selectedTools=plan_data.get("selected_tools", []),
            executionPath=plan_data.get("executionPath", []),
            reasonSummary=(plan_data.get("reason", "") or "")[:500] or None,
            dispatchSteps=[
                {"tool": s.get("tool", ""), "serviceId": s.get("service", ""),
                 "latencyMs": s.get("latency_ms")}
                for s in dispatch_steps
            ],
            totalExpectedCalls=plan_data.get("dispatch", {}).get("total_calls", 0),
            evidenceRef=f"event/planner_decision/{iter_num}",
        ))

        # Iteration verifier snapshot
        ver_dict = None
        if ver:
            ver_data = ver.get("data", {})
            ver_checks = ver_data.get("checks", [])
            ver_issues = ver_data.get("issues", [])
            ver_dict = asdict(IterationVerifier(
                iteration=iter_num,
                status=ver_data.get("status", "FAILED"),
                summary=(ver_data.get("summary", "") or "")[:500] or None,
                checks=[
                    {
                        "check": c.get("check", "overall_verification"),
                        "status": c.get("status", "UNKNOWN"),
                        "evidenceRefs": c.get("evidence_refs", []),
                    }
                    for c in ver_checks
                ],
                issues=ver_issues if isinstance(ver_issues, list) else [],
                issueCount=len(ver_issues) if isinstance(ver_issues, list) else 0,
                evidenceRef=f"event/verifier_result/{iter_num}",
            ))

        # Iteration tool calls
        tc_list = []
        failed_ids = []
        for tc in iter_tc:
            d = tc.get("data", {})
            cid = d.get("call_id", "")
            success = d.get("success", True)
            if not success:
                failed_ids.append(cid)
            tc_list.append(asdict(IterationToolCall(
                callId=cid,
                toolName=d.get("tool_name", ""),
                serviceId=d.get("service_id", ""),
                channel=d.get("channel", ""),
                success=d.get("success", True),
                serviceName=d.get("service_name") or None,
                transport=d.get("transport") or None,
                arguments=d.get("arguments", {}),
                resultPreview=d.get("result") or None,
                resultHash=d.get("result_hash") or None,
                error=d.get("error") or None,
                latencyMs=d.get("latency_ms"),
                timestamp=d.get("timestamp"),
            )))

        # Iteration exception
        iter_exc = None
        has_tool_err = len(failed_ids) > 0
        has_ver_fail = ver and ver.get("data", {}).get("status") == "FAILED"
        if has_tool_err or has_ver_fail:
            ver_issues = (ver.get("data", {}).get("issues", []) if ver else [])
            iter_exc = asdict(IterationException(
                hasToolError=has_tool_err,
                hasVerifierFailure=has_ver_fail,
                failedToolCalls=failed_ids,
                verifierIssues=ver_issues if isinstance(ver_issues, list) else [],
            ))

        iteration_snapshots.append(IterationSnapshot(
            iterationNumber=iter_num,
            states=iter_state_ids,
            planner=planner_dict,
            verifier=ver_dict,
            toolCalls=tc_list,
            exception=iter_exc,
        ))

    return StateMachineTrace(
        states=[asdict(s) for s in states],
        transitions=[asdict(t) for t in transitions],
        iterations=[asdict(itr) for itr in iteration_snapshots],
        totalIterations=trace.get("iterations", 0),
        finalStatus=final_status,
        elapsedMs=trace.get("elapsed_ms", 0),
        strategy=strategy,
    )


def _state_to_trigger(prev_id: str, next_state: str, states: list[StateNode]) -> str:
    """Infer transition trigger from previous state name and next state."""
    prev_node = next((s for s in states if s.stateId == prev_id), None)
    prev_name = prev_node.state if prev_node else ""
    # Map (prev, next) → trigger name
    mapping = {
        ("INITIALIZING", "DISCOVERING"): "simulation_started",
        ("DISCOVERING", "PLANNING"): "services_discovered",
        ("PLANNING", "EXECUTING"): "planner_decision_emitted",
        ("EXECUTING", "VERIFYING"): "tools_executed",
        ("VERIFYING", "PASSED"): "verification_passed",
        ("VERIFYING", "FAILED"): "verification_failed",
        ("FAILED", "RETRYING"): "planner_retry",
        ("RETRYING", "PLANNING"): "planner_retry",
    }
    key = (prev_name, next_state)
    return mapping.get(key, {
        "PASSED": "verification_complete",
        "RETRYING": "planner_retry",
    }.get(next_state, "verification_complete"))


def _first_ts(events: list[dict]) -> float | None:
    for e in events:
        ts = e.get("timestamp")
        if ts:
            return float(ts)
    return None


def _last_ts(events: list[dict]) -> float | None:
    for e in reversed(events):
        ts = e.get("timestamp")
        if ts:
            return float(ts)
    return None


# ---- Evidence -----------------------------------------------------------


def _build_evidence(pipeline_result: Any | None) -> EvidenceSnapshot | None:
    if pipeline_result is None:
        return None
    try:
        card = pipeline_result.card
        report = pipeline_result.report
        return EvidenceSnapshot(
            evidenceId=card.evidence_id,
            evidenceFingerprint=card.evidence_fingerprint,
            checkerStatus=report.overall_status,
            completeness=getattr(report, "completeness", None),
            missingEvidenceCategories=pipeline_result.bundle.missing_evidence,
            evidenceRef=None,  # set by caller after save_to_dir
        )
    except Exception:
        return None


# ---- Provenance ---------------------------------------------------------


def _build_provenance(
    session_id: str,
    trace_version: str,
    trace_hash: str,
    config_hash: str,
    events: list[dict],
    compiler_version: str | None = None,
    created_at: str = "",
) -> Provenance:
    tc_prov: list[dict] = []
    for e in events:
        if e.get("type") != "tool_call_record":
            continue
        d = e.get("data", {})
        tc_prov.append(asdict(ToolCallProvenance(
            callId=d.get("call_id", ""),
            resultHash=d.get("result_hash") or None,
            toolName=d.get("tool_name", ""),
            serviceId=d.get("service_id", ""),
            channel=d.get("channel", ""),
            timestamp=d.get("timestamp"),
        )))

    return Provenance(
        sourceSessionId=session_id,
        sourceTraceVersion=trace_version,
        traceHash=trace_hash,
        configSnapshotHash=config_hash,
        artifactHash="",  # placeholder — filled after assembly
        compilerVersion=compiler_version,
        createdAt=created_at,
        toolCallProvenance=tc_prov,
    )


# ---- Solidification Report ----------------------------------------------


def _build_solidification_report(
    trace: dict,
    smt: StateMachineTrace,
    evidence: EvidenceSnapshot | None,
) -> SolidificationReport:
    gates: list[GateResult] = []

    # Gate 1: sufficient iterations
    strategy = trace.get("strategy", {})
    required = strategy.get("minIterations", 1)
    actual = trace.get("iterations", 0)
    iter_ok = actual >= required
    gates.append(GateResult(
        gate="sufficientIterations",
        passed=iter_ok,
        detail=f"实际迭代 {actual} 轮，最少要求 {required} 轮",
        remediation="" if iter_ok else f"需至少 {required} 轮迭代，当前仅 {actual} 轮",
    ))

    # Gate 2: verifier passed (last iteration)
    last_ver_status = "UNKNOWN"
    for itr in smt.iterations:
        ver = itr.get("verifier")
        if ver:
            last_ver_status = ver.get("status", "UNKNOWN")
    ver_ok = last_ver_status == "PASSED"
    gates.append(GateResult(
        gate="verifierPassed",
        passed=ver_ok,
        detail=f"最后一轮 Verifier 状态: {last_ver_status}",
        remediation="" if ver_ok else "需 Verifier 通过所有断言检查",
    ))

    # Gate 3: evidence complete
    ev_completeness = evidence.completeness if evidence else None
    ev_ok = ev_completeness == "COMPLETE"
    gates.append(GateResult(
        gate="evidenceComplete",
        passed=ev_ok,
        detail=f"证据完整性: {ev_completeness or '未运行 evidence pipeline'}",
        remediation="" if ev_ok else "运行证据分析 pipeline 并确保所有证据维度通过检查",
    ))

    # Gate 4: no unresolved tool errors
    tool_events = [e for e in trace.get("events", []) if e.get("type") == "tool_call_record"]
    failed = [e for e in tool_events if not e.get("data", {}).get("success")]
    tool_ok = len(failed) == 0
    failed_ids = [f.get("data", {}).get("call_id", "?") for f in failed]
    gates.append(GateResult(
        gate="noUnresolvedToolErrors",
        passed=tool_ok,
        detail=f"失败工具调用: {len(failed)} 个 ({failed_ids})",
        remediation="" if tool_ok else f"检查并修复 {len(failed)} 个失败的工具调用",
    ))

    # Gate 5: no infrastructure errors
    infra_ok = smt.finalStatus != "UNKNOWN"
    gates.append(GateResult(
        gate="noInfrastructureErrors",
        passed=infra_ok,
        detail=f"仿真最终状态: {smt.finalStatus}",
        remediation="" if infra_ok else "仿真未正常结束，检查基础设施日志",
    ))

    # Gate 6: real MCP calls present
    real_calls = [e for e in tool_events if e.get("data", {}).get("channel") == "real_mcp"]
    real_ok = len(real_calls) > 0
    gates.append(GateResult(
        gate="realMcpCallsPresent",
        passed=real_ok,
        detail=f"真实 MCP 调用: {len(real_calls)} 次 (sandbox-only 不可固化)",
        remediation="" if real_ok else "至少需要一次 real_mcp 通道的 MCP 调用",
    ))

    all_pass = all(g.passed for g in gates)

    conditions = {
        "sufficientIterations": {"passed": iter_ok, "value": actual, "required": required,
                                  "detail": gates[0].detail},
        "verifierPassed": {"passed": ver_ok, "status": last_ver_status,
                            "detail": gates[1].detail},
        "evidenceComplete": {"passed": ev_ok, "completenessStatus": ev_completeness,
                              "missingCategories": evidence.missingEvidenceCategories if evidence else [],
                              "detail": gates[2].detail},
        "noUnresolvedToolErrors": {"passed": tool_ok, "failedCalls": len(failed),
                                    "failedCallIds": failed_ids, "detail": gates[3].detail},
        "noInfrastructureErrors": {"passed": infra_ok, "detail": gates[4].detail},
        "realMcpCallsPresent": {"passed": real_ok, "realMcpCallCount": len(real_calls),
                                 "detail": gates[5].detail},
    }

    return SolidificationReport(
        solidifiable=all_pass,
        conditions=conditions,
        gates=[asdict(g) for g in gates],
    )


# ---- WriteBack Draft ----------------------------------------------------


def _build_write_back(
    trace: dict,
    smt: StateMachineTrace,
    scenario: ScenarioInfo,
    meta_app: MetaAppInfo,
    prov: Provenance,
) -> WriteBackDraft:
    meta = trace.get("metadata", {})
    cfg = meta.get("config_snapshot", {})

    # Existing fields
    existing = asdict(WriteBackExistingFields(
        name=meta_app.appName,
        subtitle=scenario.description or "",
        services=cfg.get("serviceIds", []),
        inputName="仿真输入",
        outputName="仿真输出",
        outputVisualization=True,
        submitButtonText="开始构建",
        des=scenario.sourceDescription,
    ))

    # New fields (require schema migration)
    new = asdict(WriteBackNewFields(
        artifactSpecJson="",  # filled by caller after serialization
        sourceSessionId=prov.sourceSessionId,
        traceHash=prov.traceHash,
        artifactHash=prov.artifactHash,
        schemaVersion="0.1.0",
    ))

    # Tools extracted from state machine
    seen_tools: set[str] = set()
    tools: list[dict] = []
    for itr in smt.iterations:
        for tc in itr.get("toolCalls", []):
            name = tc.get("toolName", "")
            if name and name not in seen_tools:
                seen_tools.add(name)
                tools.append({
                    "name": name,
                    "description": tc.get("serviceId", ""),
                })

    return WriteBackDraft(
        existingFields=existing,
        newFields=new,
        tools=tools,
    )
