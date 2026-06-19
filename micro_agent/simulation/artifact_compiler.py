"""ArtifactSpec v0.3 Compiler — trace → 结论型元应用产物 + 可选 goldenPath

从 v1.0.0 TraceRecord 编译 ArtifactSpec。完整过程 trace 保留在 data/traces/；
产物只保存想定结论、服务契约、固化报告、可选黄金路径。

顶层结构（5 个收敛核心字段 + 2 个结论型附属字段）：
  核心：parsedIntent / serviceContracts / goldenPath / solidificationReport / artifactMeta
  附属：evidence（证据检查结论摘要，非完整检查过程）
        writeBackDraft（平台目录回写草稿，纯派生字段）
两个附属字段均为结论/引用型，不携带逐步过程明细，故保留于产物主体。

用法:
    from micro_agent.simulation.artifact_compiler import compile_artifact_spec
    spec = compile_artifact_spec(trace_dict, pipeline_result)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from micro_agent.scenario.schema import (
    ScenarioParsed,
    ScenarioSource,
    normalize_scenario_parsed,
)

DEFAULT_FALLBACK_POLICY = {
    "onInputMismatch": "planner_replan",
    "onServiceUnavailable": "planner_replan",
    "onToolFailure": "retry_then_replan",
    "onAssertionFail": "verifier_then_planner",
    "onSafetyViolation": "abort_and_user_confirm",
}

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MetaAppInfo:
    appName: str
    domain: str
    mode: str = "production"
    appId: Optional[str] = None


@dataclass
class _TraceSummary:
    """内部：从 trace 推导的构建摘要，不写入产物主体。"""

    finalStatus: str = "UNKNOWN"
    totalIterations: int = 0
    elapsedMs: int = 0
    strategy: dict = field(default_factory=dict)
    tool_call_events: list[dict] = field(default_factory=list)


@dataclass
class EvidenceSnapshot:
    evidenceId: Optional[str] = None
    checkerStatus: Optional[str] = None
    completeness: Optional[str] = None
    missingEvidenceCategories: list[str] = field(default_factory=list)


@dataclass
class ObservedTool:
    toolName: str
    callCount: int
    successCount: int = 0
    failureCount: int = 0
    successRate: float = 0.0
    avgLatencyMs: Optional[float] = None
    evidenceRefs: list[str] = field(default_factory=list)


@dataclass
class ServiceContract:
    serviceId: str
    serviceName: str
    channel: Optional[str] = None
    transport: Optional[str] = None
    declaredTools: list[dict] = field(default_factory=list)
    observedTools: list[dict] = field(default_factory=list)
    totalCalls: int = 0
    overallSuccessRate: Optional[float] = None


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str
    remediation: str = ""


@dataclass
class SolidificationReport:
    solidifiable: bool
    gates: list[dict] = field(default_factory=list)
    conditions: dict = field(default_factory=dict)
    goldenPathExtractable: bool = False
    goldenPathReason: str = ""
    remediation: list[str] = field(default_factory=list)


@dataclass
class WriteBackDraft:
    targetTable: str = "service_apis"
    existingFields: dict = field(default_factory=dict)
    newFields: dict = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)


@dataclass
class ArtifactSpec:
    """ArtifactSpec v0.3 — 结论 + 可选 goldenPath。"""

    schemaVersion: str = "0.3.0"
    parsedIntent: dict = field(default_factory=dict)
    serviceContracts: list[dict] = field(default_factory=list)
    goldenPath: Optional[dict] = None
    solidificationReport: dict = field(default_factory=dict)
    artifactMeta: dict = field(default_factory=dict)
    evidence: Optional[dict] = None
    writeBackDraft: Optional[dict] = None

    @property
    def solidifiable(self) -> bool:
        return bool(self.solidificationReport.get("solidifiable"))

    @property
    def artifactId(self) -> str:
        return str(self.artifactMeta.get("artifactId", ""))

    @property
    def sourceSessionId(self) -> str:
        return str(self.artifactMeta.get("sourceSessionId", ""))

    def to_dict(self) -> dict:
        return asdict(self)


# 向后兼容：测试/旧代码可能仍引用
ExecutionTrace = _TraceSummary
ExecutionStep = dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_artifact_spec(
    trace: dict,
    pipeline_result: Any | None = None,
    *,
    schema_version: str = "0.3.0",
    compiler_version: str | None = None,
) -> ArtifactSpec:
    """Compile ArtifactSpec v0.3 from a v1.0.0 trace dict."""
    meta = trace.get("metadata", {})
    runtime = meta.get("runtime", {})
    trace_ver = runtime.get("trace_version") or meta.get("trace_version", "")
    if trace_ver != "v1.0.0":
        raise ValueError(
            f"ArtifactSpec compiler requires v1.0.0 traces, got {trace_ver!r}"
        )

    session_id = trace.get("session_id", "")
    events: list[dict] = trace.get("events", [])
    summary = _derive_trace_summary(events, trace)
    meta_app = _build_meta_app(trace)
    parsed_intent = _build_parsed_intent(trace, meta_app, session_id)
    evidence = _build_evidence(pipeline_result)
    service_contracts = _build_service_contracts(trace)

    created_at = _ts_to_iso(trace.get("created_at"))
    config_snapshot = meta.get("config_snapshot", {})
    trace_hash = _sha256(json.dumps(trace, ensure_ascii=False, sort_keys=True))
    config_hash = _sha256(json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True))

    solidification = _build_solidification_report(trace, summary, evidence)

    golden_path: Optional[dict] = None
    if solidification.solidifiable:
        golden_path, gp_ok, gp_reason, gp_remediation = _extract_golden_path(
            trace,
            events,
            session_id,
            parsed_intent,
            service_contracts,
            evidence,
        )
        solidification.goldenPathExtractable = gp_ok
        solidification.goldenPathReason = gp_reason
        solidification.remediation.extend(gp_remediation)
    else:
        solidification.goldenPathExtractable = False
        solidification.goldenPathReason = "solidification gates failed"
        solidification.remediation = _collect_gate_remediation(solidification.gates)

    artifact_meta = _build_artifact_meta(
        session_id=session_id,
        trace_hash=trace_hash,
        config_hash=config_hash,
        meta_app=meta_app,
        summary=summary,
        evidence=evidence,
        parsed_intent=parsed_intent,
        compiler_version=compiler_version,
        created_at=created_at,
        artifact_hash="",
    )

    write_back = _build_write_back(trace, golden_path, parsed_intent, meta_app, artifact_meta)

    spec = ArtifactSpec(
        schemaVersion=schema_version,
        parsedIntent=parsed_intent,
        serviceContracts=[asdict(c) for c in service_contracts],
        goldenPath=golden_path,
        solidificationReport=asdict(solidification),
        artifactMeta=artifact_meta,
        evidence=asdict(evidence) if evidence else None,
        writeBackDraft=asdict(write_back) if write_back else None,
    )

    d = spec.to_dict()
    d["artifactMeta"]["artifactHash"] = ""
    art_hash = _sha256(json.dumps(d, ensure_ascii=False, sort_keys=True))
    spec.artifactMeta["artifactHash"] = art_hash
    spec.artifactMeta["artifactId"] = _short_id("art", f"{session_id}:{art_hash}")
    if spec.writeBackDraft:
        spec.writeBackDraft["newFields"]["artifactHash"] = art_hash
        spec.writeBackDraft["newFields"]["schemaVersion"] = schema_version

    return spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_to_iso(ts: float | int | None) -> str:
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
    h = _sha256(seed)
    return f"{prefix}-{h[:6]}-{h[8:16]}"


def _truncate(val: Any, max_len: int) -> str:
    if val is None:
        return ""
    s = str(val)
    return s[:max_len] if len(s) > max_len else s


def _collect_gate_remediation(gates: list[dict]) -> list[str]:
    items: list[str] = []
    for g in gates:
        if not g.get("passed") and g.get("remediation"):
            items.append(str(g["remediation"]))
    return items


def _tool_call_records(events: list[dict]) -> list[dict]:
    """单一真相源：所有 tool_call_record 事件的 data（按出现顺序）。"""
    return [
        e.get("data", {})
        for e in events
        if e.get("type") == "tool_call_record"
    ]


def _call_identity(rec: dict) -> str:
    """同一逻辑工具的标识：tool_name + service_id（用于判断失败是否被后续成功覆盖）。"""
    tool = str(rec.get("tool_name") or rec.get("tool") or "")
    svc = str(rec.get("service_id") or rec.get("service") or "")
    return f"{svc}::{tool}"


def _unresolved_failures(records: list[dict]) -> list[dict]:
    """未解决的失败调用。

    一次失败若被同一 (service, tool) 的后续成功调用覆盖（修复重试成功），
    则视为已解决，不计入。返回仍未被任何后续成功覆盖的失败记录。
    Planner/Verifier 多轮修复架构下，前轮失败 + 后轮成功是正常成功路径。
    """
    # 每个 identity 最后一次成功的位置
    last_success_idx: dict[str, int] = {}
    for idx, rec in enumerate(records):
        if rec.get("success"):
            last_success_idx[_call_identity(rec)] = idx

    unresolved: list[dict] = []
    for idx, rec in enumerate(records):
        if rec.get("success"):
            continue
        ident = _call_identity(rec)
        # 同工具是否在该失败之后又成功过？是则已被修复覆盖。
        if last_success_idx.get(ident, -1) > idx:
            continue
        unresolved.append(rec)
    return unresolved


# ---------------------------------------------------------------------------
# parsedIntent
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


def _build_scenario(trace: dict, meta_app: MetaAppInfo) -> ScenarioParsed:
    meta = trace.get("metadata", {})
    cfg = meta.get("config_snapshot", {})
    scenario_desc = cfg.get("scenarioDescription", "") or ""
    events = trace.get("events", [])

    parsed_data: dict = {}
    for ev in events:
        if ev.get("type") == "scenario_parsed":
            data = ev.get("data")
            if isinstance(data, dict) and data:
                parsed_data = data

    if parsed_data:
        scenario = normalize_scenario_parsed(
            parsed_data,
            raw_user_input=scenario_desc,
            parser_model=parsed_data.get("parserModel"),
            parsed_at=parsed_data.get("parsedAt"),
            intake_session_id=parsed_data.get("intakeSessionId"),
        )
        scenario.domain = meta_app.domain or scenario.domain
    else:
        scenario = ScenarioParsed(
            goal=scenario_desc[:300] if scenario_desc else meta_app.appName,
            domain=meta_app.domain,
            description=scenario_desc,
        )

    if scenario.source is None:
        scenario.source = ScenarioSource()
    if not scenario.source.rawUserInput:
        scenario.source.rawUserInput = scenario_desc

    return scenario


def _build_parsed_intent(trace: dict, meta_app: MetaAppInfo, session_id: str) -> dict:
    """轻量想定：不含完整追问对话，仅 sourceRef。"""
    scenario = _build_scenario(trace, meta_app)
    source = scenario.source or ScenarioSource()
    return {
        "goal": scenario.goal,
        "constraints": list(scenario.constraints),
        "acceptanceCriteria": list(scenario.acceptanceCriteria),
        "domain": scenario.domain,
        "description": scenario.description,
        "sourceRef": {
            "traceRef": session_id,
            "intakeSessionRef": source.intakeSessionId,
            "parserModel": source.parserModel,
            "parsedAt": source.parsedAt,
        },
    }


# ---------------------------------------------------------------------------
# Trace summary (internal)
# ---------------------------------------------------------------------------


def _derive_trace_summary(events: list[dict], trace: dict) -> _TraceSummary:
    complete_events = [e for e in events if e.get("type") == "complete"]
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

    tool_call_events = [e for e in events if e.get("type") == "tool_call_record"]
    return _TraceSummary(
        finalStatus=final_status,
        totalIterations=trace.get("iterations", 0),
        elapsedMs=trace.get("elapsed_ms", 0),
        strategy=trace.get("strategy", {}),
        tool_call_events=tool_call_events,
    )


def _build_execution_trace(events: list[dict], trace: dict) -> _TraceSummary:
    """向后兼容别名。"""
    return _derive_trace_summary(events, trace)


# ---------------------------------------------------------------------------
# Service contracts
# ---------------------------------------------------------------------------


def _build_service_contracts(trace: dict) -> list[ServiceContract]:
    meta = trace.get("metadata", {})
    cfg = meta.get("config_snapshot", {})
    services_meta = cfg.get("servicesMeta", [])

    calls_by_service: dict[str, list[dict]] = {}
    for ev in trace.get("events", []):
        if ev.get("type") != "tool_call_record":
            continue
        d = ev.get("data", {})
        sid = str(d.get("service_id", ""))
        calls_by_service.setdefault(sid, []).append(d)

    contracts: list[ServiceContract] = []
    for svc in services_meta:
        sid = str(svc.get("id", ""))
        declared = [
            {
                "toolId": t.get("id") or None,
                "name": t.get("name", ""),
                "description": t.get("description") or None,
            }
            for t in svc.get("tools", [])
            if t.get("name")
        ]
        calls = calls_by_service.get(sid, [])
        observed = _aggregate_observed_tools(calls)
        total = len(calls)
        succeeded = sum(1 for c in calls if c.get("success"))
        channel = calls[0].get("channel") if calls else None
        transport = calls[0].get("transport") if calls else None

        contracts.append(ServiceContract(
            serviceId=sid,
            serviceName=svc.get("name", ""),
            channel=channel,
            transport=transport,
            declaredTools=declared,
            observedTools=observed,
            totalCalls=total,
            overallSuccessRate=round(succeeded / total, 4) if total else None,
        ))

    return contracts


def _aggregate_observed_tools(calls: list[dict]) -> list[dict]:
    by_tool: dict[str, list[dict]] = {}
    for c in calls:
        by_tool.setdefault(c.get("tool_name", ""), []).append(c)

    observed: list[dict] = []
    for tool_name in sorted(by_tool):
        group = by_tool[tool_name]
        success = sum(1 for c in group if c.get("success"))
        failure = len(group) - success
        latencies = [
            c["latency_ms"] for c in group
            if isinstance(c.get("latency_ms"), (int, float))
        ]
        avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else None
        observed.append(asdict(ObservedTool(
            toolName=tool_name,
            callCount=len(group),
            successCount=success,
            failureCount=failure,
            successRate=round(success / len(group), 4) if group else 0.0,
            avgLatencyMs=avg_latency,
            evidenceRefs=[c.get("call_id", "") for c in group if c.get("call_id")],
        )))
    return observed


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _build_evidence(pipeline_result: Any | None) -> Optional[EvidenceSnapshot]:
    if pipeline_result is None:
        return None
    try:
        card = pipeline_result.card
        report = pipeline_result.report
        return EvidenceSnapshot(
            evidenceId=card.evidence_id,
            checkerStatus=report.overall_status,
            completeness=getattr(report, "completeness", None),
            missingEvidenceCategories=list(pipeline_result.bundle.missing_evidence),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Solidification
# ---------------------------------------------------------------------------


def _build_solidification_report(
    trace: dict,
    summary: _TraceSummary,
    evidence: Optional[EvidenceSnapshot],
) -> SolidificationReport:
    gates: list[GateResult] = []
    events = trace.get("events", [])

    strategy = trace.get("strategy", {})
    stability_req = strategy.get("stabilityPasses")
    verifier_passes = sum(
        1 for e in events
        if e.get("type") == "verifier_result"
        and e.get("data", {}).get("status") == "PASSED"
    )
    if stability_req is not None:
        try:
            required = max(1, int(stability_req))
        except (TypeError, ValueError):
            required = 1
        iter_ok = verifier_passes >= required
        suff_detail = f"验证通过 {verifier_passes} 次，要求至少 {required} 次"
        suff_value = verifier_passes
    else:
        try:
            required = max(1, int(strategy.get("minIterations", 1)))
        except (TypeError, ValueError):
            required = 1
        actual = trace.get("iterations", 0)
        iter_ok = actual >= required
        suff_detail = f"实际迭代 {actual} 轮，最少要求 {required} 轮"
        suff_value = actual

    gates.append(GateResult(
        gate="sufficientIterations", passed=iter_ok, detail=suff_detail,
        remediation="" if iter_ok else f"需至少 {required} 轮/次",
    ))

    last_ver_status = "UNKNOWN"
    for ev in events:
        if ev.get("type") == "verifier_result":
            last_ver_status = ev.get("data", {}).get("status", "UNKNOWN")
    ver_ok = last_ver_status == "PASSED"
    gates.append(GateResult(
        gate="verifierPassed", passed=ver_ok,
        detail=f"最后一轮 Verifier: {last_ver_status}",
        remediation="" if ver_ok else "需 Verifier 通过",
    ))

    ev_completeness = evidence.completeness if evidence else None
    ev_ok = ev_completeness == "COMPLETE"
    gates.append(GateResult(
        gate="evidenceComplete", passed=ev_ok,
        detail=f"证据完整性: {ev_completeness or '未运行'}",
        remediation="" if ev_ok else "运行证据 pipeline",
    ))

    tool_events = summary.tool_call_events or [
        e for e in events if e.get("type") == "tool_call_record"
    ]
    records = [e.get("data", {}) for e in tool_events]
    unresolved = _unresolved_failures(records)
    total_failed = sum(1 for r in records if not r.get("success"))
    resolved = total_failed - len(unresolved)
    tool_ok = len(unresolved) == 0
    failed_ids = [f.get("call_id", "?") for f in unresolved]
    if total_failed and tool_ok:
        tool_detail = f"失败调用 {total_failed} 个，均被后续成功重试覆盖（未解决: 0）"
    else:
        tool_detail = f"未解决的失败调用: {len(unresolved)} 个"
    gates.append(GateResult(
        gate="noUnresolvedToolErrors", passed=tool_ok,
        detail=tool_detail,
        remediation="" if tool_ok else f"修复 {len(unresolved)} 个未解决的失败调用",
    ))

    infra_ok = summary.finalStatus != "UNKNOWN"
    gates.append(GateResult(
        gate="noInfrastructureErrors", passed=infra_ok,
        detail=f"最终状态: {summary.finalStatus}",
        remediation="" if infra_ok else "仿真未正常结束",
    ))

    real_calls = [
        e for e in tool_events
        if e.get("data", {}).get("channel") == "real_mcp"
    ]
    real_ok = len(real_calls) > 0
    gates.append(GateResult(
        gate="realMcpCallsPresent", passed=real_ok,
        detail=f"真实 MCP 调用: {len(real_calls)} 次",
        remediation="" if real_ok else "至少需一次 real_mcp 调用",
    ))

    all_pass = all(g.passed for g in gates)
    conditions = {
        "sufficientIterations": {
            "passed": iter_ok, "value": suff_value, "required": required,
            "detail": gates[0].detail,
        },
        "verifierPassed": {
            "passed": ver_ok, "status": last_ver_status, "detail": gates[1].detail,
        },
        "evidenceComplete": {
            "passed": ev_ok, "completenessStatus": ev_completeness,
            "missingCategories": evidence.missingEvidenceCategories if evidence else [],
            "detail": gates[2].detail,
        },
        "noUnresolvedToolErrors": {
            "passed": tool_ok, "unresolvedFailures": len(unresolved),
            "totalFailures": total_failed, "resolvedByRetry": resolved,
            "failedCallIds": failed_ids, "detail": gates[3].detail,
        },
        "noInfrastructureErrors": {"passed": infra_ok, "detail": gates[4].detail},
        "realMcpCallsPresent": {
            "passed": real_ok, "realMcpCallCount": len(real_calls), "detail": gates[5].detail,
        },
    }

    remediation: list[str] = []
    if not all_pass:
        remediation = _collect_gate_remediation([asdict(g) for g in gates])

    return SolidificationReport(
        solidifiable=all_pass,
        gates=[asdict(g) for g in gates],
        conditions=conditions,
        goldenPathExtractable=False,
        goldenPathReason="",
        remediation=remediation,
    )


# ---------------------------------------------------------------------------
# Golden path extraction
# ---------------------------------------------------------------------------


def _find_final_passed_verifier(events: list[dict]) -> Optional[dict]:
    last: Optional[dict] = None
    for ev in events:
        if ev.get("type") == "verifier_result":
            if ev.get("data", {}).get("status") == "PASSED":
                last = ev
    return last


def _find_planner_for_iteration(events: list[dict], iteration: int) -> Optional[dict]:
    last: Optional[dict] = None
    for ev in events:
        if ev.get("type") == "planner_decision":
            data = ev.get("data", {})
            if data.get("iteration") == iteration:
                last = ev
    return last


def _is_real_successful_call(detail: dict) -> bool:
    if not detail.get("success"):
        return False
    channel = str(detail.get("channel", "")).lower()
    if channel in ("sandbox", "mock", "demo"):
        return False
    return channel == "real_mcp"


def _order_tool_details(
    planner: dict,
    successful_details: list[dict],
) -> list[dict]:
    path = planner.get("executionPath") or []
    ordered: list[dict] = []
    used: set[str] = set()

    for node in path:
        node_s = str(node)
        for d in successful_details:
            cid = d.get("call_id", "")
            if cid in used:
                continue
            tool = str(d.get("tool", ""))
            svc = str(d.get("service", ""))
            if (
                node_s == tool or node_s == svc
                or tool in node_s or svc in node_s
                or node_s in tool or node_s in svc
            ):
                ordered.append(d)
                used.add(cid)
                break

    for d in successful_details:
        cid = d.get("call_id", "")
        if cid not in used:
            ordered.append(d)
            used.add(cid)

    return ordered


def _slim_input_binding(
    arguments: dict,
    parsed_intent: dict,
    prior_outputs: list[tuple[str, str]],
) -> dict:
    """从真实数据流推断参数来源，不按位置编造。

    prior_outputs: [(stepId, result_preview_text), ...] 前序成功调用的输出文本。
    - 值出现在 parsedIntent 文本中            → {"$from": "parsedIntent"}
    - 值出现在某前序 step 的输出文本中         → {"$from": "step_X.output"}
    - 过长值                                   → {"$truncated": ...}
    - 其余                                     → 字面量（无法追溯则保留原值）
    """
    if not isinstance(arguments, dict) or not arguments:
        return {}
    binding: dict[str, Any] = {}
    intent_text = " ".join([
        parsed_intent.get("goal", ""),
        parsed_intent.get("description", ""),
        " ".join(parsed_intent.get("constraints") or []),
        " ".join(parsed_intent.get("acceptanceCriteria") or []),
    ])
    for key, val in arguments.items():
        if str(key).startswith("_"):
            continue
        if isinstance(val, (str, int, float, bool)):
            s = str(val)
            if len(s) > 120:
                binding[key] = {"$truncated": s[:120]}
                continue
            if s and s in intent_text:
                binding[key] = {"$from": "parsedIntent"}
                continue
            traced_step = None
            if s:
                for step_id, out_text in prior_outputs:
                    if out_text and s in out_text:
                        traced_step = step_id
                        break
            if traced_step:
                binding[key] = {"$from": f"{traced_step}.output"}
            else:
                binding[key] = val
        elif isinstance(val, dict):
            binding[key] = {"$ref": "object", "keys": list(val.keys())[:8]}
        else:
            binding[key] = {"$ref": type(val).__name__}
    return binding


def _output_slots(result_preview: Any) -> list[str]:
    if not result_preview:
        return ["result"]
    text = result_preview if isinstance(result_preview, str) else str(result_preview)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj:
            return list(obj.keys())[:10]
    except (json.JSONDecodeError, TypeError):
        # result_preview 常被截断（orchestrator 截到 200 字符），整体 parse 会失败。
        # 退化为从截断 JSON 文本中抽取顶层键名，尽力而为。
        keys = re.findall(r'"([A-Za-z_][\w-]*)"\s*:', text)
        if keys:
            seen: list[str] = []
            for k in keys:
                if k not in seen:
                    seen.append(k)
            return seen[:10]
    return ["result"]


def _node_matches_tool(node: str, tool: str) -> bool:
    node_s, tool_s = str(node), str(tool)
    return bool(tool_s) and (node_s == tool_s or tool_s in node_s or node_s in tool_s)


def _order_is_subsequence(step_tools: list[str], expected_path: list[str]) -> bool:
    """golden 步骤工具序列是否为审定 executionPath 的有序子序列。"""
    i = 0
    for node in expected_path:
        if i >= len(step_tools):
            break
        if _node_matches_tool(node, step_tools[i]):
            i += 1
    return i == len(step_tools)


def _build_golden_assertions(
    steps: list[dict],
    parsed_intent: dict,
    *,
    verifier_passed: bool,
    expected_path: list[str],
    forbidden_tools: set[str],
    evidence_refs: list[str],
) -> list[dict]:
    """生成可从 trace 真正核验的断言；无法核验的标 unknown，不强行 pass。"""
    assertions: list[dict] = []
    tool_names = [s["toolName"] for s in steps]
    step_refs = [r for s in steps for r in (s.get("_callId"),) if r]

    # L1: required tool called —— 主干非空且每步都有工具名
    assertions.append({
        "assertionId": "a_l1_required_tools",
        "level": "L1_structure",
        "type": "required_tool_called",
        "result": "pass" if tool_names and all(tool_names) else "fail",
        "evidenceRefs": step_refs or evidence_refs[: len(tool_names)],
    })

    # L1: forbidden tool not called —— 主干中不得出现 mock/sandbox/失败工具
    used_forbidden = sorted(t for t in tool_names if t in forbidden_tools)
    assertions.append({
        "assertionId": "a_l1_forbidden_tools",
        "level": "L1_structure",
        "type": "forbidden_tool_not_called",
        "result": "fail" if used_forbidden else "pass",
        "evidenceRefs": step_refs,
    })

    # L1: tool call success —— golden 仅纳入成功 real_mcp 调用，逐项核验
    assertions.append({
        "assertionId": "a_l1_tool_success",
        "level": "L1_structure",
        "type": "tool_call_success",
        "result": "pass" if steps else "fail",
        "evidenceRefs": step_refs or evidence_refs,
    })

    # L1: tool order —— 步骤序列须是审定 executionPath 的有序子序列
    if len(tool_names) >= 2:
        if not expected_path:
            order_result = "unknown"
        elif _order_is_subsequence(tool_names, expected_path):
            order_result = "pass"
        else:
            order_result = "fail"
        assertions.append({
            "assertionId": "a_l1_tool_order",
            "level": "L1_structure",
            "type": "tool_order",
            "result": order_result,
            "evidenceRefs": step_refs,
        })

    # L2: output consumed —— 是否存在某步参数真正绑定到前序步骤输出
    chained = [
        s for s in steps
        if any(
            isinstance(v, dict) and str(v.get("$from", "")).endswith(".output")
            for v in (s.get("inputBinding") or {}).values()
        )
    ]
    if len(steps) < 2:
        chain_result = "unknown"  # 单步无链路可证
    elif chained:
        chain_result = "pass"
    else:
        chain_result = "unknown"  # 多步但未观测到输出消费，不臆断
    assertions.append({
        "assertionId": "a_l2_output_chain",
        "level": "L2_dataflow",
        "type": "output_consumed",
        "result": chain_result,
        "evidenceRefs": [s["_callId"] for s in chained if s.get("_callId")],
    })

    # L2: param binding traceable —— 每个非空绑定的参数能溯源到 intent / 前序 step
    bound_steps = [s for s in steps if s.get("inputBinding")]
    if not bound_steps:
        bind_result = "unknown"
    else:
        all_traceable = all(
            all(
                isinstance(v, dict) and ("$from" in v or "$ref" in v or "$truncated" in v)
                for v in s["inputBinding"].values()
            )
            for s in bound_steps
        )
        any_provenance = any(
            isinstance(v, dict) and "$from" in v
            for s in bound_steps for v in s["inputBinding"].values()
        )
        bind_result = "pass" if (all_traceable and any_provenance) else "unknown"
    assertions.append({
        "assertionId": "a_l2_param_binding",
        "level": "L2_dataflow",
        "type": "param_binding_traceable",
        "result": bind_result,
        "evidenceRefs": step_refs,
    })

    # L3: acceptance satisfied —— 仅当有可检验收标准且 Verifier 终判通过才 pass
    acceptance = parsed_intent.get("acceptanceCriteria") or []
    if verifier_passed and acceptance:
        sem_result = "pass"
    else:
        sem_result = "unknown"  # 无验收标准时，Verifier 整体通过不足以断言 acceptance
    assertions.append({
        "assertionId": "a_l3_verifier",
        "level": "L3_semantic",
        "type": "acceptance_satisfied",
        "result": sem_result,
        "evidenceRefs": evidence_refs[:1],
    })

    return assertions


def _contract_ref_service_id(contract_ref: str) -> str:
    """从 'serviceContracts.<sid>' 解析出 serviceId。"""
    prefix = "serviceContracts."
    if contract_ref.startswith(prefix):
        sid = contract_ref[len(prefix):]
        return "" if sid == "unknown" else sid
    return ""


def _build_applicability(
    parsed_intent: dict,
    service_contracts: list[ServiceContract],
    steps: list[dict],
) -> dict:
    # requiredServices 来自每个 step 的 contractRef（step 上没有 serviceId 字段）
    required_services = sorted({
        sid
        for s in steps
        if (sid := _contract_ref_service_id(s.get("contractRef", "")))
    })
    if not required_services:
        required_services = sorted(
            c.serviceId for c in service_contracts if c.totalCalls > 0
        )

    input_sig: list[str] = []
    if parsed_intent.get("goal"):
        input_sig.append(f"goal:{_truncate(parsed_intent['goal'], 80)}")
    for c in parsed_intent.get("constraints") or []:
        input_sig.append(f"constraint:{_truncate(c, 80)}")

    required_outputs = list(parsed_intent.get("acceptanceCriteria") or [])
    if not required_outputs and steps:
        last_slots = steps[-1].get("outputSlots") or ["result"]
        required_outputs = [f"final:{slot}" for slot in last_slots]

    # entryGuards 依据该 trace 实际情况推导，而非硬编码
    entry_guards = ["verifier_passed", "real_mcp_only"]
    if parsed_intent.get("constraints"):
        entry_guards.append("hard_constraints_present")
    if parsed_intent.get("acceptanceCriteria"):
        entry_guards.append("acceptance_criteria_defined")

    return {
        "inputSignature": input_sig,
        "requiredOutputs": required_outputs,
        "requiredServices": required_services,
        "hardConstraints": list(parsed_intent.get("constraints") or []),
        "entryGuards": entry_guards,
    }


def _extract_golden_path(
    trace: dict,
    events: list[dict],
    session_id: str,
    parsed_intent: dict,
    service_contracts: list[ServiceContract],
    evidence: Optional[EvidenceSnapshot],
) -> tuple[Optional[dict], bool, str, list[str]]:
    remediation: list[str] = []
    final_verifier = _find_final_passed_verifier(events)
    if not final_verifier:
        return None, False, "missing final passed verifier_result", [
            "需 Verifier 最终通过后才能抽取黄金路径",
        ]

    vdata = final_verifier.get("data", {})
    planner = vdata.get("plannerDecision")
    iteration = vdata.get("iteration")
    if not planner and iteration is not None:
        pev = _find_planner_for_iteration(events, iteration)
        planner = pev.get("data") if pev else None
    if not planner:
        return None, False, "missing final passed plan", [
            "缺少最终通过的 planner_decision / plannerDecision",
        ]

    details = planner.get("tool_call_details") or []
    successful = [d for d in details if _is_real_successful_call(d)]
    if not successful:
        successful = [
            d for d in details
            if d.get("success") and str(d.get("channel", "")).lower() == "real_mcp"
        ]
    if not successful:
        return None, False, "no successful real_mcp calls in final plan", [
            "最终方案中无成功的 real_mcp 调用",
        ]

    ordered = _order_tool_details(planner, successful)

    # forbidden 工具：整条 trace 中出现于非 real_mcp 通道或失败的工具名
    forbidden_tools: set[str] = set()
    for d in _tool_call_records(events):
        name = str(d.get("tool_name") or d.get("tool") or "")
        if not name:
            continue
        ch = str(d.get("channel", "")).lower()
        if ch in ("sandbox", "mock", "demo") or not d.get("success"):
            forbidden_tools.add(name)
    # 但成功 real_mcp 主干内的工具不算 forbidden（同名工具修复后成功）
    forbidden_tools -= {str(d.get("tool", "")) for d in ordered}

    expected_path = [str(n) for n in (planner.get("executionPath") or [])]

    steps: list[dict] = []
    evidence_refs: list[str] = []
    if evidence and evidence.evidenceId:
        evidence_refs.append(evidence.evidenceId)
    prior_outputs: list[tuple[str, str]] = []

    for idx, detail in enumerate(ordered, start=1):
        call_id = detail.get("call_id", "")
        tool_name = detail.get("tool", "")
        service_id = str(detail.get("service", ""))
        if not tool_name:
            remediation.append(f"step {idx}: missing toolName")
            continue
        binding = _slim_input_binding(
            detail.get("arguments") or {},
            parsed_intent,
            prior_outputs,
        )
        if detail.get("arguments") and not binding:
            remediation.append(f"step {idx}: missing inputBinding for non-empty arguments")

        step_id = f"step_{idx}"
        if call_id:
            evidence_refs.append(call_id)
        steps.append({
            "stepId": step_id,
            "toolName": tool_name,
            "contractRef": f"serviceContracts.{service_id or 'unknown'}",
            "inputBinding": binding,
            "outputSlots": _output_slots(detail.get("result_preview")),
            "assertionRefs": [],
            "onFailure": "retry_then_replan",
            "_callId": call_id,
        })
        prior_outputs.append((step_id, str(detail.get("result_preview") or "")))

    if not steps:
        return None, False, "no extractable golden steps", remediation or [
            "无法从最终方案构建步骤",
        ]

    if remediation:
        return None, False, "missing stepId / inputBinding / final passed plan", remediation

    assertions = _build_golden_assertions(
        steps,
        parsed_intent,
        verifier_passed=True,
        expected_path=expected_path,
        forbidden_tools=forbidden_tools,
        evidence_refs=evidence_refs,
    )

    # 任一可核验断言判负 → 主干不可信，不抽取
    failed_assertions = [a["assertionId"] for a in assertions if a["result"] == "fail"]
    if failed_assertions:
        return None, False, "golden path assertions failed", [
            f"断言未通过: {', '.join(failed_assertions)}",
        ]

    # 逐步关联断言：通用 L1 + 与该步相关的 L2（若该步消费了前序输出）
    common_refs = [
        a["assertionId"] for a in assertions
        if a["assertionId"] in (
            "a_l1_required_tools", "a_l1_tool_success",
            "a_l1_forbidden_tools", "a_l1_tool_order",
        )
    ]
    for step in steps:
        refs = list(common_refs)
        has_chain = any(
            isinstance(v, dict) and str(v.get("$from", "")).endswith(".output")
            for v in (step.get("inputBinding") or {}).values()
        )
        if has_chain:
            refs.append("a_l2_output_chain")
        step["assertionRefs"] = refs
        step.pop("_callId", None)

    applicability = _build_applicability(parsed_intent, service_contracts, steps)
    golden = {
        "sourceTraceRef": session_id,
        "applicability": applicability,
        "steps": steps,
        "assertions": assertions,
        "evidenceRefs": list(dict.fromkeys(evidence_refs)),
        "fallbackPolicy": dict(DEFAULT_FALLBACK_POLICY),
    }
    return golden, True, "final passed MCP call spine extracted", []


# ---------------------------------------------------------------------------
# artifactMeta / writeBack
# ---------------------------------------------------------------------------


def _build_artifact_meta(
    *,
    session_id: str,
    trace_hash: str,
    config_hash: str,
    meta_app: MetaAppInfo,
    summary: _TraceSummary,
    evidence: Optional[EvidenceSnapshot],
    parsed_intent: dict,
    compiler_version: str | None,
    created_at: str,
    artifact_hash: str,
) -> dict:
    source_ref = parsed_intent.get("sourceRef") or {}
    return {
        "artifactId": _short_id("art", f"{session_id}:{trace_hash}"),
        "sourceSessionId": session_id,
        "createdAt": created_at,
        "appName": meta_app.appName,
        "domain": meta_app.domain,
        "mode": meta_app.mode,
        "appId": meta_app.appId,
        "traceRef": session_id,
        "traceHash": trace_hash,
        "configSnapshotHash": config_hash,
        "artifactHash": artifact_hash,
        "compilerVersion": compiler_version,
        "evidenceRef": evidence.evidenceId if evidence else None,
        "intakeSessionRef": source_ref.get("intakeSessionRef"),
        "buildSummary": {
            "totalIterations": summary.totalIterations,
            "finalStatus": summary.finalStatus,
            "elapsedMs": summary.elapsedMs,
        },
    }


def _build_provenance(
    session_id: str,
    trace_version: str,
    trace_hash: str,
    config_hash: str,
    events: list[dict],
    compiler_version: str | None = None,
    created_at: str = "",
) -> dict:
    """向后兼容：返回 artifactMeta 风格溯源字段，不含逐步 toolCallProvenance。"""
    return {
        "sourceSessionId": session_id,
        "sourceTraceVersion": trace_version,
        "traceHash": trace_hash,
        "configSnapshotHash": config_hash,
        "artifactHash": "",
        "compilerVersion": compiler_version,
        "createdAt": created_at,
    }


def _build_write_back(
    trace: dict,
    golden_path: Optional[dict],
    parsed_intent: dict,
    meta_app: MetaAppInfo,
    artifact_meta: dict,
) -> WriteBackDraft:
    meta = trace.get("metadata", {})
    cfg = meta.get("config_snapshot", {})
    description = parsed_intent.get("description", "")

    existing = {
        "name": meta_app.appName,
        "subtitle": description[:120] if description else "",
        "services": cfg.get("serviceIds", []),
        "inputName": "仿真输入",
        "outputName": "仿真输出",
        "outputVisualization": True,
        "submitButtonText": "开始构建",
        "des": description,
    }
    new_fields = {
        "artifactSpecJson": "",
        "sourceSessionId": artifact_meta.get("sourceSessionId", ""),
        "traceHash": artifact_meta.get("traceHash", ""),
        "artifactHash": artifact_meta.get("artifactHash", ""),
        "schemaVersion": "0.3.0",
    }

    tools: list[dict] = []
    seen: set[str] = set()
    if golden_path:
        for step in golden_path.get("steps") or []:
            name = step.get("toolName", "")
            ref = step.get("contractRef", "")
            if name and name not in seen:
                seen.add(name)
                tools.append({"name": name, "description": ref})

    return WriteBackDraft(existingFields=existing, newFields=new_fields, tools=tools)


__all__ = [
    "ArtifactSpec",
    "compile_artifact_spec",
    "ExecutionTrace",
    "ExecutionStep",
    "SolidificationReport",
    "_build_execution_trace",
    "_build_solidification_report",
    "_build_provenance",
    "_build_write_back",
    "_build_evidence",
    "_build_service_contracts",
    "_extract_golden_path",
    "_sha256",
    "_short_id",
    "_ts_to_iso",
]
