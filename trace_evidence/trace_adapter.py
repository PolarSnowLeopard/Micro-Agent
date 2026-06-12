"""Trace Evidence Adapter — 仅支持 v1.0.0 结构化轨迹。

从 tool_call_record、planner_decision、verifier_result 等事件提取证据；
不从 log 文本回退解析。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from .sanitize import redact_secrets
except ImportError:
    from sanitize import redact_secrets


@dataclass
class ToolCallEvidence:
    """从 trace log 提取的工具调用证据"""
    tool_name: str
    service_id: str  # 从 tool_name 前缀推断 (e.g. mcp-demo-openfda)
    timestamp: float
    direction: str  # "call" | "return"
    # §6 required: source/confidence provenance
    source: str = "persisted_metadata"
    confidence: str = "original"
    channel: str = "unknown"  # real_mcp | sandbox | mock | unknown
    # Traceability: index into trace["events"] for verification
    trace_event_index: Optional[int] = None
    # 以下字段当前 trace 中无法获取，显式标为 missing
    arguments: Optional[dict] = None
    result: Optional[str] = None
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    result_hash: Optional[str] = None
    missing_evidence: list[str] = field(default_factory=list)


@dataclass
class ServiceEvidence:
    """服务探测证据"""
    service_id: str
    status: str
    latency_ms: int
    channel: str  # real_mcp | sandbox | mock | unknown
    tools: list[str]
    timestamp: float
    source: str = "original_trace"  # from trace service events directly
    confidence: str = "original"
    # Traceability: index into trace["events"] for verification
    trace_event_index: Optional[int] = None


@dataclass
class PlannerThoughtEvidence:
    """Planner 思考过程证据"""
    content: str  # 原始文本 or reasoning
    timestamp: float
    iteration: Optional[int] = None
    source: str = "original_trace"
    confidence: str = "original"
    # v1 structured fields (from planner_decision event)
    candidate_tools: Optional[list[str]] = None
    selected_tools: Optional[list[str]] = None


@dataclass
class PhaseEvidence:
    """阶段执行证据"""
    phase: str
    status: str
    timestamp: float
    source: str = "original_trace"
    confidence: str = "original"
    # Traceability: index into trace["events"] for verification
    trace_event_index: Optional[int] = None


@dataclass
class IterationEvidence:
    """迭代执行证据"""
    iteration: int
    status: str
    timestamp: float
    source: str = "original_trace"
    confidence: str = "original"
    # Traceability: index into trace["events"] for verification
    trace_event_index: Optional[int] = None


@dataclass
class VerificationEvidence:
    """验证结果证据"""
    status: str  # PASSED / FAILED / UNKNOWN
    reason: Optional[str] = None
    iteration: Optional[int] = None
    timestamp: Optional[float] = None
    raw_text: Optional[str] = None
    source: str = "original_trace"
    confidence: str = "original"
    missing_evidence: list[str] = field(default_factory=list)


@dataclass
class CompleteEvidence:
    """运行完成证据"""
    success: bool
    iterations: int
    elapsed_ms: int
    execution_path: list[str]
    tool_channels: list[dict]
    timestamp: float
    source: str = "original_trace"
    confidence: str = "original"


@dataclass
class TraceEvidenceBundle:
    """一次仿真运行的完整证据包"""
    session_id: str
    app_name: str
    domain: str
    mode: str
    strategy: dict

    services: list[ServiceEvidence] = field(default_factory=list)
    tool_calls: list[ToolCallEvidence] = field(default_factory=list)
    planner_thoughts: list[PlannerThoughtEvidence] = field(default_factory=list)
    phases: list[PhaseEvidence] = field(default_factory=list)
    iterations: list[IterationEvidence] = field(default_factory=list)
    verification: Optional[VerificationEvidence] = None
    completion: Optional[CompleteEvidence] = None

    # 全局缺失标注
    missing_evidence: list[str] = field(default_factory=list)

    # Raw metadata from trace (for consistency checks)
    raw_metadata: dict = field(default_factory=dict)

    # Diagnostics: why events were dropped (for observability, not evidence)
    diagnostics: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class TraceEvidenceAdapter:
    """从 v1 结构化 trace JSON 提取证据"""

    def __init__(self, trace: dict):
        if not isinstance(trace, dict):
            raise TypeError(
                f"Trace must be a JSON object (dict), got {type(trace).__name__}. "
                "Expected format: {\"events\": [...], \"metadata\": {...}}"
            )
        self.trace = trace
        events = trace.get("events", [])
        if not isinstance(events, list):
            raise ValueError(
                f"trace events must be a list, got {type(events).__name__}"
            )
        self.events = events
        self._known_service_ids: set[str] = self._build_service_id_set()
        meta = trace.get("metadata")
        if meta is None:
            raise ValueError("trace metadata is required for v1.0.0")
        if not isinstance(meta, dict):
            raise ValueError(f"trace metadata must be a dict, got {type(meta).__name__}")
        ver = meta.get("trace_version") or (meta.get("runtime") or {}).get("trace_version")
        if ver != "v1.0.0":
            raise ValueError(f"Unsupported or missing trace_version {ver!r}; expected v1.0.0")

    @classmethod
    def from_file(cls, path: str | Path) -> "TraceEvidenceAdapter":
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Invalid JSON in trace file: {e.msg} (line {e.lineno}, col {e.colno})"
                ) from e
        return cls(data)

    def extract(self) -> TraceEvidenceBundle:
        """主入口：提取完整证据包"""
        bundle = TraceEvidenceBundle(
            session_id=self.trace.get("session_id", "unknown"),
            app_name=self.trace.get("app_name", ""),
            domain=self.trace.get("domain", ""),
            mode=self.trace.get("mode", ""),
            strategy=self.trace.get("strategy", {}),
        )
        # Preserve raw metadata for consistency checks
        bundle.raw_metadata = self.trace.get("metadata", {}) or {}

        # Filter: only process valid event dicts with required fields
        raw_count = len(self.events)
        valid_events = [
            ev for ev in self.events
            if isinstance(ev, dict) and "type" in ev and "timestamp" in ev
            and isinstance(ev.get("data"), dict)
        ]
        dropped = raw_count - len(valid_events)
        if dropped > 0:
            bundle.diagnostics.append({
                "level": "warning",
                "code": "events_dropped",
                "message": f"{dropped}/{raw_count} events dropped (missing type/timestamp/data)",
                "dropped_count": dropped,
                "total_count": raw_count,
            })
        self.events = valid_events

        bundle.services = self._extract_services()
        bundle.tool_calls = self._extract_tool_calls()
        # Cross-reference: propagate known service channels to tool calls
        self._enrich_tool_call_channels(bundle)
        bundle.planner_thoughts = self._extract_planner_thoughts()
        bundle.phases = self._extract_phases()
        bundle.iterations = self._extract_iterations()
        bundle.verification = self._extract_verification()
        bundle.completion = self._extract_completion()
        bundle.missing_evidence = self._assess_gaps(bundle)

        return bundle

    def _extract_services(self) -> list[ServiceEvidence]:
        results = []
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] == "service":
                d = ev["data"]
                sid = d.get("id")
                if not sid:
                    continue  # skip malformed service events
                results.append(ServiceEvidence(
                    service_id=sid,
                    status=d.get("status", "unknown"),
                    latency_ms=d.get("latency", 0),
                    channel=d.get("channel", "unknown"),
                    tools=d.get("tools", []),
                    timestamp=ev["timestamp"],
                    trace_event_index=_ev_idx,
                ))
        return results

    def _extract_tool_calls(self) -> list[ToolCallEvidence]:
        """从 tool_call_record 事件提取工具调用证据（call + return 对）。"""
        results = []
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] != "tool_call_record":
                continue
            d = ev["data"]
            ts = ev.get("timestamp", d.get("timestamp", 0))
            channel = d.get("channel", "unknown")
            # 生成 call + return 两条
            results.append(ToolCallEvidence(
                tool_name=d["tool_name"],
                service_id=d.get("service_id", self._infer_service_id(d["tool_name"])),
                timestamp=ts,
                direction="call",
                source="persisted_metadata",
                confidence="original",
                channel=channel,
                arguments=d.get("arguments"),
                latency_ms=d.get("latency_ms"),
                trace_event_index=_ev_idx,
            ))
            results.append(ToolCallEvidence(
                tool_name=d["tool_name"],
                service_id=d.get("service_id", self._infer_service_id(d["tool_name"])),
                timestamp=ts,
                direction="return",
                source="persisted_metadata",
                confidence="original",
                channel=channel,
                result=d.get("result"),
                error=d.get("error"),
                latency_ms=d.get("latency_ms"),
                result_hash=d.get("result_hash"),
                trace_event_index=_ev_idx,
            ))
        return results

    def _enrich_tool_call_channels(self, bundle: TraceEvidenceBundle) -> None:
        """Cross-reference tool calls against discovered services to infer channel.

        When a tool call's service_id matches a discovered service, we can
        legitimately propagate that service's known channel (e.g. 'mcp') to
        the tool call. This is NOT fabrication — it's inference from data
        already present in the same trace.
        """
        # Build service_id → channel lookup from discovered services
        service_channels: dict[str, str] = {}
        for svc in bundle.services:
            if svc.channel and svc.channel != "unknown":
                service_channels[svc.service_id] = svc.channel

        if not service_channels:
            return  # no channel info to propagate

        # Enrich tool calls that still have unknown channel
        for tc in bundle.tool_calls:
            if tc.channel == "unknown" and tc.service_id in service_channels:
                tc.channel = service_channels[tc.service_id]
                # Upgrade confidence: this is a derived inference, not raw log parsing
                if tc.confidence == "inferred":
                    tc.confidence = "derived"
            elif tc.channel == "unknown" and tc.service_id == "internal":
                tc.channel = "local"

    def _extract_planner_thoughts(self) -> list[PlannerThoughtEvidence]:
        results = []
        for ev in self.events:
            if ev.get("type") != "planner_decision":
                continue
            d = ev.get("data", {})
            reason = d.get("reason") or d.get("reasoning") or ""
            n_sel = len(d.get("selected_tools", []))
            n_cand = len(d.get("candidate_tools", []))
            results.append(PlannerThoughtEvidence(
                content=reason or f"Selected {n_sel} tools from {n_cand} candidates",
                timestamp=ev["timestamp"],
                iteration=d.get("iteration"),
                candidate_tools=d.get("candidate_tools"),
                selected_tools=d.get("selected_tools"),
            ))
        return results

    def _extract_phases(self) -> list[PhaseEvidence]:
        results = []
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] == "phase":
                d = ev["data"]
                phase = d.get("phase")
                if not phase:
                    continue
                results.append(PhaseEvidence(
                    phase=phase,
                    status=d.get("status", "unknown"),
                    timestamp=ev["timestamp"],
                    trace_event_index=_ev_idx,
                ))
        return results

    def _extract_iterations(self) -> list[IterationEvidence]:
        results = []
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] == "iteration":
                d = ev["data"]
                iteration = d.get("iteration")
                if iteration is None:
                    continue
                results.append(IterationEvidence(
                    iteration=iteration,
                    status=d.get("status", "unknown"),
                    timestamp=ev["timestamp"],
                    trace_event_index=_ev_idx,
                ))
        return results

    def _extract_verification(self) -> Optional[VerificationEvidence]:
        for ev in reversed(self.events):
            if ev.get("type") != "verifier_result":
                continue
            d = ev.get("data", {})
            status = d.get("status", "UNKNOWN")
            reason = d.get("summary") or d.get("reason") or None
            checks = d.get("checks", [])
            if not reason and checks:
                failed = [c for c in checks if c.get("status") not in ("PASS", "PASSED")]
                reason = (
                    "; ".join(f"{c.get('check', '?')}:{c['status']}" for c in failed[:5])
                    if failed
                    else f"All {len(checks)} checks passed"
                )
            return VerificationEvidence(
                status=status,
                reason=reason,
                iteration=d.get("iteration"),
                timestamp=ev.get("timestamp"),
                missing_evidence=[],
            )
        return VerificationEvidence(
            status="UNKNOWN",
            missing_evidence=["verifier_result"],
        )

    def _extract_completion(self) -> Optional[CompleteEvidence]:
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] == "complete":
                d = ev["data"]
                metrics = d.get("metrics", {})
                result = d.get("result", {})
                return CompleteEvidence(
                    success=d.get("success", False),
                    iterations=metrics.get("iterations", 0),
                    elapsed_ms=metrics.get("elapsedMs", 0),
                    execution_path=result.get("executionPath", []),
                    tool_channels=result.get("toolChannels", []),
                    timestamp=ev["timestamp"],
                )
        return None

    def _assess_gaps(self, bundle: TraceEvidenceBundle) -> list[str]:
        """评估证据缺口"""
        gaps = []

        if not bundle.services:
            gaps.append("no_service_discovery_events")
        if not any(ev.get("type") == "tool_call_record" for ev in self.events):
            gaps.append("no_tool_call_record_events")
        if not bundle.planner_thoughts:
            gaps.append("no_planner_decision_events")
        if bundle.verification and bundle.verification.status == "UNKNOWN":
            gaps.append("no_verifier_result_event")
        if not self.trace.get("metadata"):
            gaps.append("trace_metadata_empty")

        return gaps

    def _build_service_id_set(self) -> set[str]:
        """Extract known service IDs from service discovery events in the trace."""
        ids = set()
        events = self.trace.get("events", [])
        if not isinstance(events, list):
            return ids
        for ev in events:
            if (isinstance(ev, dict) and ev.get("type") == "service"
                    and isinstance(ev.get("data"), dict)):
                sid = ev["data"].get("id")
                if sid:
                    ids.add(sid)
        return ids

    def _infer_service_id(self, tool_name: str) -> str:
        if not tool_name:
            return "internal"
        best_match = ""
        for sid in self._known_service_ids:
            if tool_name.startswith(sid + "_") and len(sid) > len(best_match):
                best_match = sid
        if best_match:
            return best_match
        if tool_name in ("terminate", "finish", "abort", "noop"):
            return "internal"
        return "unresolved"
