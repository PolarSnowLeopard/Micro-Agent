"""P0-② Trace Evidence Adapter

从原始 trace JSON 提取结构化证据事件。
对已有 trace 的 log-text 做最大限度结构化提取，同时显式标注 missing_evidence。

设计原则:
- 不伪造证据: 只从 trace 中实际存在的数据提取
- 信息缺失标 missing_evidence 字段
- 输出标准化 EvidenceEvent 列表，供 evidence_card / checker 消费
"""

from __future__ import annotations

import json
import re
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
    source: str = "inferred_from_log"  # original_trace | persisted_metadata | adapter | inferred_from_log
    confidence: str = "inferred"  # original | derived | inferred | missing
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
    source: str = "inferred_from_log"  # or "original_trace" for structured events
    confidence: str = "inferred"  # or "original" for structured events
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
    source: str = "inferred_from_log"  # verification is parsed from log text
    confidence: str = "inferred"
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
    """从原始 trace JSON 提取结构化证据"""

    # Fallback regex for legacy "mcp-demo-" prefix (used only if service list unavailable)
    _TOOL_NAME_RE_FALLBACK = re.compile(r'^(mcp-demo-[a-z]+(?:-[a-z]+)*)_(.+)$')

    def __init__(self, trace: dict):
        if not isinstance(trace, dict):
            raise TypeError(
                f"Trace must be a JSON object (dict), got {type(trace).__name__}. "
                "Expected format: {\"events\": [...], \"metadata\": {...}}"
            )
        self.trace = trace
        events = trace.get("events", [])
        self.events = events if isinstance(events, list) else []
        # Build service prefix set from actual service discovery events
        self._known_service_ids: set[str] = self._build_service_id_set()

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
        """从 log events 和 tool_call_record events 中提取工具调用证据。

        优先使用结构化的 tool_call_record（来自 P0-① 增强）；
        如果没有 tool_call_record，回退到从 log text 解析。
        """
        # 先尝试结构化记录（来自 enhanced trace）
        structured = self._extract_structured_tool_calls()
        if structured:
            return structured
        # 回退: 从 log text 解析
        return self._extract_tool_calls_from_logs()

    def _extract_structured_tool_calls(self) -> list[ToolCallEvidence]:
        """从 tool_call_record 类型事件提取（P0-① enhanced traces）"""
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

    def _extract_tool_calls_from_logs(self) -> list[ToolCallEvidence]:
        """回退: 从 log events 文本中提取工具调用/返回对"""
        results = []
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] != "log":
                continue
            msg = ev["data"].get("message", "")
            ts = ev["timestamp"]

            if "[Planner] 调用工具:" in msg:
                parts = msg.split("[Planner] 调用工具:", 1)
                tool_name = parts[1].strip() if len(parts) > 1 else ""
                if not tool_name:
                    continue  # skip malformed log entries
                service_id = self._infer_service_id(tool_name)
                results.append(ToolCallEvidence(
                    tool_name=tool_name,
                    service_id=service_id,
                    timestamp=ts,
                    direction="call",
                    source="derived_from_log",
                    confidence="derived",
                    missing_evidence=["arguments", "latency_ms"],
                    trace_event_index=_ev_idx,
                ))
            elif "[Planner] 工具返回:" in msg:
                parts = msg.split("[Planner] 工具返回:", 1)
                tool_name = parts[1].strip() if len(parts) > 1 else ""
                if not tool_name:
                    continue
                service_id = self._infer_service_id(tool_name)
                results.append(ToolCallEvidence(
                    tool_name=tool_name,
                    service_id=service_id,
                    timestamp=ts,
                    direction="return",
                    source="derived_from_log",
                    confidence="derived",
                    missing_evidence=["result", "error", "latency_ms"],
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
        """提取 Planner 思考过程。优先读取结构化 planner_decision 事件（v1），
        fallback 到 log 文本解析。
        """
        results = []
        # === Priority 1: structured planner_decision events (v1) ===
        for ev in self.events:
            if ev.get("type") == "planner_decision":
                d = ev.get("data", {})
                reasoning = d.get("reasoning", "")
                results.append(PlannerThoughtEvidence(
                    content=reasoning or f"Selected {len(d.get('selected_tools', []))} tools from {len(d.get('candidate_tools', []))} candidates",
                    timestamp=ev["timestamp"],
                    iteration=d.get("iteration"),
                    source="original_trace",
                    confidence="original",
                    candidate_tools=d.get("candidate_tools"),
                    selected_tools=d.get("selected_tools"),
                ))
        if results:
            return results

        # === Priority 2: log text with [Planner] 思考: (legacy) ===
        current_iteration = 1
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] == "iteration":
                current_iteration = ev["data"].get("iteration", current_iteration)
            if ev["type"] != "log":
                continue
            msg = ev["data"].get("message", "")
            if "[Planner] 思考:" in msg:
                content = msg.split("[Planner] 思考:")[1].strip()
                results.append(PlannerThoughtEvidence(
                    content=content,
                    timestamp=ev["timestamp"],
                    iteration=current_iteration,
                    source="derived_from_log",
                    confidence="derived",
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
        """提取验证结果。优先读取结构化 verifier_result 事件（v1），
        fallback 到 log 文本解析或 completion.success 推断。
        """
        # === Priority 1: structured verifier_result event (v1) ===
        for ev in self.events:
            if ev.get("type") == "verifier_result":
                d = ev.get("data", {})
                status = d.get("status", "UNKNOWN")
                # Prefer "summary" (rich text), fall back to "reason"
                reason = d.get("summary") or d.get("reason") or None
                checks = d.get("checks", [])
                # Build reason from checks if not explicitly provided
                if not reason and checks:
                    failed = [c for c in checks if c.get("status") not in ("PASS", "PASSED")]
                    if failed:
                        reason = "; ".join(
                            f"{c.get('check', c.get('name', '?'))}:{c['status']}"
                            for c in failed[:5]
                        )
                    else:
                        reason = f"All {len(checks)} checks passed"
                return VerificationEvidence(
                    status=status,
                    reason=reason,
                    timestamp=ev.get("timestamp"),
                    raw_text=None,
                    source="original_trace",
                    confidence="original",
                    missing_evidence=[],
                )

        # === Priority 2: log text with PASSED/FAILED (legacy) ===
        verification_texts = []
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] != "log":
                continue
            msg = ev["data"].get("message", "")
            if "[Planner] 思考:" in msg and ("PASSED" in msg or "FAILED" in msg):
                verification_texts.append((msg, ev["timestamp"]))

        if not verification_texts:
            # Fallback: infer from completion event success field
            for ev in self.events:
                if ev["type"] == "complete":
                    d = ev["data"]
                    success = d.get("success")
                    if success is not None:
                        status = "PASSED" if success else "FAILED"
                        return VerificationEvidence(
                            status=status,
                            reason=f"Inferred from completion event (success={success})",
                            timestamp=ev.get("timestamp"),
                            confidence="derived",
                        )
            # No completion event either
            return VerificationEvidence(
                status="UNKNOWN",
                missing_evidence=["verifier_result", "verifier_reason"],
            )

        # 取最后一个包含 PASSED/FAILED 的思考
        last_text, last_ts = verification_texts[-1]
        status = "PASSED" if "PASSED" in last_text else "FAILED"
        # 尝试提取原因
        reason_match = re.search(r'(PASSED|FAILED)[：:\s]*(.+?)(?:\n|$)', last_text)
        reason = reason_match.group(2).strip() if reason_match else None

        return VerificationEvidence(
            status=status,
            reason=reason,
            timestamp=last_ts,
            raw_text=redact_secrets(last_text[:500]),  # 保留原文作为证据，脱敏处理
            source="derived_from_log",
            confidence="derived",
            missing_evidence=["structured_verifier_output"],
        )

    def _extract_completion(self) -> Optional[CompleteEvidence]:
        for _ev_idx, ev in enumerate(self.events):
            if ev["type"] == "complete":
                d = ev["data"]
                metrics = d.get("metrics", {})
                result = d.get("result", {})
                execution_path = result.get("executionPath", [])
                tool_channels = result.get("toolChannels", [])

                # Synthesize execution_path from phase events if not explicit
                if not execution_path:
                    execution_path = self._synthesize_execution_path()

                # Synthesize tool_channels from service discovery if not explicit
                if not tool_channels:
                    tool_channels = self._synthesize_tool_channels()

                return CompleteEvidence(
                    success=d.get("success", False),
                    iterations=metrics.get("iterations", 0),
                    elapsed_ms=metrics.get("elapsedMs", 0),
                    execution_path=execution_path,
                    tool_channels=tool_channels,
                    timestamp=ev["timestamp"],
                )
        return None

    def _synthesize_execution_path(self) -> list[str]:
        """Reconstruct execution_path from phase events when not explicitly stored."""
        seen = []
        for ev in self.events:
            if ev.get("type") == "phase":
                phase_name = ev.get("data", {}).get("phase", "")
                status = ev.get("data", {}).get("status", "")
                if phase_name and status == "running" and phase_name not in seen:
                    seen.append(phase_name)
        return seen

    def _synthesize_tool_channels(self) -> list[str]:
        """Reconstruct tool_channels from service discovery events."""
        channels = set()
        for ev in self.events:
            if ev.get("type") == "service":
                data = ev.get("data", {})
                # Detect channel from mcpUrl or explicit channel field
                if data.get("mcpUrl") or data.get("channel") == "mcp":
                    channels.add("mcp")
                elif data.get("httpUrl") or data.get("channel") == "http":
                    channels.add("http")
                elif data.get("channel"):
                    channels.add(data["channel"])
        return sorted(channels)

    def _assess_gaps(self, bundle: TraceEvidenceBundle) -> list[str]:
        """评估证据缺口"""
        gaps = []

        if not bundle.services:
            gaps.append("no_service_discovery_events")
        if not bundle.tool_calls:
            gaps.append("no_tool_call_events")
        else:
            # For log-parsed traces, tool args/results are inherently absent.
            # Only flag as gap if trace has structured tool_call events but they're empty.
            has_structured_tool_events = any(
                ev.get("type") == "tool_call" for ev in self.events
            )
            if has_structured_tool_events:
                if all(tc.arguments is None for tc in bundle.tool_calls):
                    gaps.append("tool_call_arguments_not_persisted")
                if all(tc.result is None for tc in bundle.tool_calls if tc.direction == "return"):
                    gaps.append("tool_call_results_not_persisted")
        if not bundle.planner_thoughts:
            gaps.append("no_planner_reasoning_captured")

        if bundle.verification and bundle.verification.status == "UNKNOWN":
            gaps.append("verification_result_not_structured")

        # metadata 缺失 — only flag for structured (enhanced) traces.
        # Log-parsed traces inherently lack metadata; flagging them is noise.
        has_structured_events = any(
            ev.get("type") in ("tool_call_record", "tool_call")
            for ev in self.events
        )
        if not self.trace.get("metadata") and has_structured_events:
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
        """从 tool_name 推断 service_id。

        Strategy (robust):
        1. Check if tool_name starts with any known service_id + "_" (longest match wins)
        2. Fallback to regex for legacy "mcp-demo-" prefix
        3. If tool_name matches known internal tools (terminate, etc.) → "internal"
        4. Otherwise → "unresolved" (makes unknown-ness visible)
        """
        if not tool_name:
            return "internal"

        # Strategy 1: longest prefix match against known services
        best_match = ""
        for sid in self._known_service_ids:
            if tool_name.startswith(sid + "_") and len(sid) > len(best_match):
                best_match = sid
        if best_match:
            return best_match

        # Strategy 2: fallback regex for legacy naming
        m = self._TOOL_NAME_RE_FALLBACK.match(tool_name)
        if m:
            return m.group(1)

        # Strategy 3: known internal tools
        if tool_name in ("terminate", "finish", "abort", "noop"):
            return "internal"

        # Strategy 4: unresolved — don't mask failures with silent "internal"
        return "unresolved"
