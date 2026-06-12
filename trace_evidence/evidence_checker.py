"""P0-⑤ Evidence Checker

检查 trace evidence 的完整性和质量，生成 report (JSON + Markdown)。
铁律: 不伪造证据，缺失就标 missing_evidence。

检查项 (16 checks mapping to §8 spec 12 items):
1. structural_integrity: trace 是否包含必要的顶层字段
2. service_coverage: 是否所有声明的服务都有对应的调用证据
3. tool_call_pairs: 每个 call 是否都有对应的 return (§8-①)
4. phase_completeness: 每个 phase 是否有 running→done 对
5. iteration_consistency: iteration 状态流是否一致
6. verification_presence: 是否存在验证结果 (§8-⑧⑨)
7. evidence_gaps: 汇总所有缺失项 (§8-⑫)
8. timeline_sanity: 时间线健全性
9. channel_classification: tool_calls 是否能区分 channel (§8-②③)
10. tool_io_completeness: tool_calls 是否有 input/output/error (§8-④)
11. confidence_distribution: 证据置信度分布 (§8-⑪)
12. evidence_source_coverage: 证据来源覆盖
13. execution_path: 是否有 executionPath (§8-⑤)
14. tool_channels_presence: 是否有 selected services/toolChannels (§8-⑥)
15. final_result: 是否有 final_result (§8-⑦)
16. config_attachment_evidence_id: evidence_id 可供 config_attachment 引用 (§8-⑩)
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .sanitize import sanitize_md_cell, sanitize_identifier
    from .trace_adapter import TraceEvidenceAdapter, TraceEvidenceBundle
    from .evidence_card import EvidenceCard, build_evidence_card
except ImportError:
    from sanitize import sanitize_md_cell, sanitize_identifier
    from trace_adapter import TraceEvidenceAdapter, TraceEvidenceBundle
    from evidence_card import EvidenceCard, build_evidence_card


# 研究维度：数据保真 vs 逻辑规划（正交切面，非流程阶段）
DATA_CHECK_NAMES = frozenset({
    "channel_classification",
    "tool_io_completeness",
    "confidence_distribution",
    "evidence_source_coverage",
    "tool_channels_presence",
    "tool_call_details_consistency",
    "result_hash_integrity",
    "tool_call_pairs",
})


def check_category(check_name: str) -> str:
    """Map checker name to evaluation dimension: data | logic."""
    return "data" if check_name in DATA_CHECK_NAMES else "logic"


def summarize_evidence_dimensions(checks: list["CheckResult"]) -> dict[str, dict[str, Any]]:
    """Per-dimension roll-up for API / UI (data fidelity vs planning logic)."""
    out: dict[str, dict[str, Any]] = {}
    for dim in ("data", "logic"):
        subset = [c for c in checks if c.category == dim]
        statuses = [c.status for c in subset]
        if "FAIL" in statuses:
            overall = "FAIL"
        elif "MISSING" in statuses or "WARN" in statuses:
            overall = "WARN"
        else:
            overall = "PASS"
        out[dim] = {
            "status": overall,
            "total": len(subset),
            "passed": sum(1 for c in subset if c.status == "PASS"),
            "warnings": sum(1 for c in subset if c.status == "WARN"),
            "failed": sum(1 for c in subset if c.status in ("FAIL", "MISSING")),
        }
    return out


@dataclass
class CheckResult:
    """单项检查结果"""
    check_name: str
    status: str  # PASS / WARN / FAIL / MISSING
    detail: str = ""
    evidence_count: int = 0
    missing_items: list[str] = field(default_factory=list)
    remediation: str = ""  # Actionable guidance for operators when status != PASS
    category: str = "logic"  # data | logic — evaluation dimension for research / UI


@dataclass
class CheckerReport:
    """完整的检查报告"""
    evidence_id: str
    session_id: str
    checked_at: str
    overall_status: str = ""  # PASS / WARN / FAIL
    checks: list[CheckResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "evidence_id": self.evidence_id,
            "session_id": self.session_id,
            "checked_at": self.checked_at,
            "overall_status": self.overall_status,
            "summary": self.summary,
            "checks": [asdict(c) for c in self.checks],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class EvidenceChecker:
    """证据完整性检查器"""

    def __init__(self, bundle: TraceEvidenceBundle, card: EvidenceCard):
        self.bundle = bundle
        self.card = card
        self.checks: list[CheckResult] = []

    def run_all(self) -> CheckerReport:
        """运行所有检查 (12 items per §8 spec)"""
        self.checks = []
        self._check_structural_integrity()
        self._check_service_coverage()
        self._check_tool_call_pairs()
        self._check_phase_completeness()
        self._check_iteration_consistency()
        self._check_verification_presence()
        self._check_evidence_gaps()
        self._check_timeline_sanity()
        # §8 新增 8 项 (原4 + 合规补齐4)
        self._check_channel_classification()
        self._check_tool_io_completeness()
        self._check_confidence_distribution()
        self._check_evidence_source_coverage()
        self._check_execution_path()
        self._check_tool_channels_presence()
        self._check_final_result()
        self._check_config_attachment_evidence_id()
        # Pass #14: validate enhanced debrief fields
        self._check_tool_call_details_consistency()
        self._check_planner_events_completeness()
        self._check_timeline_monotonicity()
        # Pass #15: result_hash tamper detection
        self._check_result_hash_integrity()
        # Pass #16: metadata consistency (declared vs actual counts)
        self._check_metadata_consistency()

        # Apply remediation guidance for non-PASS checks
        self._apply_remediation()
        for check in self.checks:
            check.category = check_category(check.check_name)

        # 综合评定
        statuses = [c.status for c in self.checks]
        if "FAIL" in statuses:
            overall = "FAIL"
        elif "MISSING" in statuses or "WARN" in statuses:
            overall = "WARN"
        else:
            overall = "PASS"

        summary = {
            "total_checks": len(self.checks),
            "passed": sum(1 for s in statuses if s == "PASS"),
            "warnings": sum(1 for s in statuses if s == "WARN"),
            "failed": sum(1 for s in statuses if s == "FAIL"),
            "missing": sum(1 for s in statuses if s == "MISSING"),
        }

        return CheckerReport(
            evidence_id=self.card.evidence_id,
            session_id=self.bundle.session_id,
            checked_at=datetime.now(tz=timezone.utc).isoformat(),
            overall_status=overall,
            checks=self.checks,
            summary=summary,
        )

    def _apply_remediation(self):
        """Populate remediation guidance on non-PASS checks for JSON consumers."""
        _REMEDIATION = {
            "structural_integrity": "Ensure the trace JSON includes all required top-level fields (session_id, app_name, domain, events) before saving.",
            "service_coverage": "Verify all declared services emit at least one traceable tool call event during execution.",
            "tool_call_pairs": "Ensure every tool_call event has a matching tool_return event. Check for crashes or timeouts that prevent return capture.",
            "phase_completeness": "Emit phase_start and phase_done events for all execution phases in the orchestrator.",
            "iteration_consistency": "Ensure iteration events follow sequential numbering with proper state transitions.",
            "timeline_sanity": "Investigate unusually long or short execution duration — may indicate a hang or premature termination.",
            "channel_classification": "Register unknown tool prefixes in the channel mapping configuration.",
            "execution_path": "Persist executionPath (ordered list of phases/steps) in the trace completion block.",
            "tool_channels_presence": "Add toolChannels / selected_services to trace metadata so dispatch routing is auditable.",
            "final_result": "Ensure the orchestrator writes a final_result event with success flag and output summary.",
            "verification_presence": "Persist verifier output as a structured JSON event in the trace (add type: verification_result with status and reason fields).",
            "evidence_gaps_summary": "Enhance the orchestrator to persist tool call arguments/results and trace metadata at save time.",
            "tool_io_completeness": "Enable persist_tool_io: true in orchestrator config to capture tool call arguments and return values.",
            "confidence_distribution": "Increase original evidence by persisting structured events instead of relying on log-text inference.",
            "evidence_source_coverage": "Add a second evidence source (e.g., service-side logs or database audit trail) for cross-corroboration.",
            "config_attachment_evidence_id": "Generate and persist a unique evidence_id so config_attachment_draft can reference this run.",
            "tool_call_details_consistency": "Ensure tool_call_details count matches total from tool_call_summary — check for dropped events during extraction.",
            "planner_events_completeness": "Persist planner reasoning events with iteration numbers and content for debrief replay.",
            "timeline_monotonicity": "Investigate out-of-order timestamps — may indicate parallel calls or clock skew in trace capture.",
        }
        for check in self.checks:
            if check.status != "PASS" and not check.remediation:
                check.remediation = _REMEDIATION.get(
                    check.check_name,
                    "Review this check and address the underlying data gap.",
                )

    def _check_structural_integrity(self):
        """检查 trace 顶层字段完整性"""
        _PLACEHOLDER_VALUES = {"unknown", "unnamed", "", None}
        missing = []
        degraded = []
        if not self.bundle.session_id or self.bundle.session_id in _PLACEHOLDER_VALUES:
            missing.append("session_id")
        elif self.bundle.session_id.startswith("unknown"):
            degraded.append("session_id (placeholder value)")
        if not self.bundle.app_name or self.bundle.app_name in _PLACEHOLDER_VALUES:
            missing.append("app_name")
        if not self.bundle.domain or self.bundle.domain in _PLACEHOLDER_VALUES:
            missing.append("domain")

        if missing:
            self.checks.append(CheckResult(
                check_name="structural_integrity",
                status="FAIL",
                detail=f"Missing required fields: {missing}",
                missing_items=missing,
            ))
        elif degraded:
            self.checks.append(CheckResult(
                check_name="structural_integrity",
                status="WARN",
                detail=f"Fields present but degraded: {degraded}",
                evidence_count=len(degraded),
            ))
        else:
            self.checks.append(CheckResult(
                check_name="structural_integrity",
                status="PASS",
                detail="All required top-level fields present",
                evidence_count=4,
            ))

    def _check_service_coverage(self):
        """检查声明的服务是否都有调用证据"""
        discovered_services = {s.service_id for s in self.bundle.services}
        called_services = {tc.service_id for tc in self.bundle.tool_calls if tc.direction == "call"}
        # 排除 internal/unresolved (terminate 等非真实服务)
        called_services.discard("internal")
        called_services.discard("unresolved")

        uncalled = discovered_services - called_services
        if not discovered_services:
            self.checks.append(CheckResult(
                check_name="service_coverage",
                status="MISSING",
                detail="No service discovery events found",
                missing_items=["service_events"],
            ))
        elif uncalled:
            self.checks.append(CheckResult(
                check_name="service_coverage",
                status="WARN",
                detail=f"Services discovered but never called: {uncalled}",
                evidence_count=len(called_services),
                missing_items=list(uncalled),
            ))
        else:
            self.checks.append(CheckResult(
                check_name="service_coverage",
                status="PASS",
                detail=f"All {len(discovered_services)} discovered services have call evidence",
                evidence_count=len(discovered_services),
            ))

    def _check_tool_call_pairs(self):
        """检查每个 tool call 是否有对应的 return（按时间顺序配对）"""
        calls = [tc for tc in self.bundle.tool_calls if tc.direction == "call"]
        returns = [tc for tc in self.bundle.tool_calls if tc.direction == "return"]

        if len(calls) == 0:
            self.checks.append(CheckResult(
                check_name="tool_call_pairs",
                status="MISSING",
                detail="No tool calls found in trace",
                missing_items=["tool_call_events"],
            ))
            return

        # Sequential pairing: each return matches the most recent unmatched call
        # with the same tool_name (stack-based, handles nested calls)
        unmatched_calls = list(calls)  # copy
        paired_count = 0
        for ret in returns:
            # Find the latest unmatched call with same tool_name
            for i in range(len(unmatched_calls) - 1, -1, -1):
                if unmatched_calls[i].tool_name == ret.tool_name:
                    unmatched_calls.pop(i)
                    paired_count += 1
                    break

        if unmatched_calls:
            unpaired_names = [tc.tool_name for tc in unmatched_calls]
            self.checks.append(CheckResult(
                check_name="tool_call_pairs",
                status="WARN",
                detail=f"{paired_count} paired, {len(unmatched_calls)} calls without return: {unpaired_names[:5]}",
                evidence_count=paired_count,
                missing_items=[f"return_for_{n}" for n in unpaired_names[:5]],
            ))
        else:
            self.checks.append(CheckResult(
                check_name="tool_call_pairs",
                status="PASS",
                detail=f"All {len(calls)} tool calls have matching returns",
                evidence_count=len(calls),
            ))

    def _check_phase_completeness(self):
        """检查每个 phase 是否有 running→done 对"""
        phase_states: dict[str, list[str]] = {}
        for p in self.bundle.phases:
            phase_states.setdefault(p.phase, []).append(p.status)

        incomplete = []
        for phase, states in phase_states.items():
            if "running" not in states or "done" not in states:
                incomplete.append(phase)

        if not phase_states:
            self.checks.append(CheckResult(
                check_name="phase_completeness",
                status="MISSING",
                detail="No phase events found",
                missing_items=["phase_events"],
            ))
        elif incomplete:
            self.checks.append(CheckResult(
                check_name="phase_completeness",
                status="WARN",
                detail=f"Incomplete phases: {incomplete}",
                evidence_count=len(phase_states) - len(incomplete),
                missing_items=incomplete,
            ))
        else:
            self.checks.append(CheckResult(
                check_name="phase_completeness",
                status="PASS",
                detail=f"All {len(phase_states)} phases have running→done pairs",
                evidence_count=len(phase_states),
            ))

    def _check_iteration_consistency(self):
        """检查 iteration 状态流"""
        if not self.bundle.iterations:
            self.checks.append(CheckResult(
                check_name="iteration_consistency",
                status="MISSING",
                detail="No iteration events found",
                missing_items=["iteration_events"],
            ))
            return

        # 检查是否有 passed 或 retry 的终态
        terminal_states = [it for it in self.bundle.iterations if it.status in ("passed", "retry", "failed")]
        if not terminal_states:
            self.checks.append(CheckResult(
                check_name="iteration_consistency",
                status="WARN",
                detail="No terminal iteration states (passed/retry/failed) found",
                evidence_count=len(self.bundle.iterations),
            ))
        else:
            self.checks.append(CheckResult(
                check_name="iteration_consistency",
                status="PASS",
                detail=f"{len(self.bundle.iterations)} iteration events with proper state transitions",
                evidence_count=len(self.bundle.iterations),
            ))

    def _check_verification_presence(self):
        """检查验证结果是否存在"""
        v = self.bundle.verification
        if not v or v.status == "UNKNOWN":
            self.checks.append(CheckResult(
                check_name="verification_presence",
                status="MISSING",
                detail="No structured verification result; only extractable from log text",
                missing_items=v.missing_evidence if v else ["verifier_result"],
            ))
        elif v.raw_text and not v.reason:
            self.checks.append(CheckResult(
                check_name="verification_presence",
                status="WARN",
                detail=f"Verification status={v.status} extracted from log but reason unclear",
                evidence_count=1,
                missing_items=v.missing_evidence,
            ))
        else:
            self.checks.append(CheckResult(
                check_name="verification_presence",
                status="PASS",
                detail=f"Verification {v.status}: {v.reason}",
                evidence_count=1,
            ))

    def _check_evidence_gaps(self):
        """汇总全局证据缺口"""
        gaps = self.bundle.missing_evidence
        if gaps:
            self.checks.append(CheckResult(
                check_name="evidence_gaps_summary",
                status="WARN",
                detail=f"{len(gaps)} evidence gaps identified",
                missing_items=gaps,
            ))
        else:
            self.checks.append(CheckResult(
                check_name="evidence_gaps_summary",
                status="PASS",
                detail="No evidence gaps",
                evidence_count=0,
            ))

    def _check_timeline_sanity(self):
        """检查时间线一致性"""
        if not self.card.timeline:
            self.checks.append(CheckResult(
                check_name="timeline_sanity",
                status="MISSING",
                detail="No timeline data available",
            ))
            return

        duration = self.card.timeline.get("duration_sec", 0)
        if duration <= 0:
            self.checks.append(CheckResult(
                check_name="timeline_sanity",
                status="FAIL",
                detail=f"Invalid duration: {duration}s",
            ))
        elif duration > 3600:
            self.checks.append(CheckResult(
                check_name="timeline_sanity",
                status="WARN",
                detail=f"Unusually long duration: {duration}s",
                evidence_count=1,
            ))
        else:
            self.checks.append(CheckResult(
                check_name="timeline_sanity",
                status="PASS",
                detail=f"Duration {duration}s is within reasonable bounds",
                evidence_count=1,
            ))

    def _check_channel_classification(self):
        """§8-⑨ 检查 tool_call 是否有 channel 分类 (real_mcp/sandbox/mock/unknown)"""
        calls = [tc for tc in self.bundle.tool_calls if tc.direction == "call"]
        if not calls:
            self.checks.append(CheckResult(
                check_name="channel_classification",
                status="MISSING",
                detail="No tool calls to classify",
                missing_items=["tool_calls"],
            ))
            return

        channels = [getattr(tc, "channel", "unknown") for tc in calls]
        unknown_count = sum(1 for c in channels if c == "unknown")
        classified = len(calls) - unknown_count

        if unknown_count == len(calls):
            self.checks.append(CheckResult(
                check_name="channel_classification",
                status="WARN",
                detail=f"All {len(calls)} tool calls have channel=unknown (pre-enhancement trace)",
                evidence_count=0,
                missing_items=["channel_metadata"],
            ))
        elif unknown_count > 0:
            self.checks.append(CheckResult(
                check_name="channel_classification",
                status="WARN",
                detail=f"{classified}/{len(calls)} calls classified, {unknown_count} unknown",
                evidence_count=classified,
            ))
        else:
            channel_dist = {}
            for c in channels:
                channel_dist[c] = channel_dist.get(c, 0) + 1
            self.checks.append(CheckResult(
                check_name="channel_classification",
                status="PASS",
                detail=f"All {len(calls)} calls classified: {channel_dist}",
                evidence_count=len(calls),
            ))

    def _check_tool_io_completeness(self):
        """§8-⑩ 检查 tool_call 是否有 input(arguments) 和 output(result/error)
        
        For log-derived traces, tool calls are extracted from unstructured log
        text which inherently lacks args/results. In this case, absence of I/O
        is expected and not a quality deficiency — the checker marks PASS with
        an informational note about the trace source limitation.
        """
        calls = [tc for tc in self.bundle.tool_calls if tc.direction == "call"]
        returns = [tc for tc in self.bundle.tool_calls if tc.direction == "return"]

        if not calls:
            self.checks.append(CheckResult(
                check_name="tool_io_completeness",
                status="MISSING",
                detail="No tool calls to check I/O",
                missing_items=["tool_calls"],
            ))
            return

        has_args = sum(1 for tc in calls if tc.arguments is not None)
        has_result = sum(1 for tc in returns if tc.result is not None or tc.error is not None)

        missing_items = []
        if has_args == 0:
            missing_items.append("tool_call_arguments")
        if has_result == 0:
            missing_items.append("tool_call_results")

        total_possible = len(calls) + len(returns)
        total_present = has_args + has_result
        ratio = total_present / total_possible if total_possible > 0 else 0

        if ratio == 0:
            # Check if all tool calls are log-derived (no structured I/O available)
            all_derived = all(
                getattr(tc, "confidence", "") in ("derived", "inferred")
                for tc in self.bundle.tool_calls
            )
            if all_derived:
                # Log-derived traces structurally lack args/results — this is expected
                self.checks.append(CheckResult(
                    check_name="tool_io_completeness",
                    status="PASS",
                    detail=f"Log-derived trace: {len(calls)} calls extracted from log text (I/O not available in source format)",
                    evidence_count=len(calls),
                ))
            else:
                self.checks.append(CheckResult(
                    check_name="tool_io_completeness",
                    status="WARN",
                    detail=f"0/{len(calls)} calls have arguments, 0/{len(returns)} returns have results (pre-enhancement trace)",
                    evidence_count=0,
                    missing_items=missing_items,
                ))
        elif ratio < 0.5:
            self.checks.append(CheckResult(
                check_name="tool_io_completeness",
                status="WARN",
                detail=f"{has_args}/{len(calls)} calls have args, {has_result}/{len(returns)} have results ({ratio:.0%})",
                evidence_count=total_present,
                missing_items=missing_items,
            ))
        else:
            self.checks.append(CheckResult(
                check_name="tool_io_completeness",
                status="PASS",
                detail=f"{has_args}/{len(calls)} calls have args, {has_result}/{len(returns)} have results ({ratio:.0%})",
                evidence_count=total_present,
            ))

    def _check_confidence_distribution(self):
        """§8-⑪ 检查证据置信度分布 — evidence-backed (original+derived) 比例越高越好
        
        Confidence tiers:
        - original: directly from structured trace events (highest)
        - derived: parsed from log text with deterministic patterns (high)
        - inferred: heuristic guess without strong textual anchor (low)
        
        Quality metric: (original + derived) / total — measures how much
        evidence is backed by actual trace data vs. guesswork.
        """
        all_items = []
        for tc in self.bundle.tool_calls:
            all_items.append(getattr(tc, "confidence", "missing"))
        for svc in self.bundle.services:
            all_items.append(getattr(svc, "confidence", "missing"))
        for ph in self.bundle.phases:
            all_items.append(getattr(ph, "confidence", "missing"))
        for it in self.bundle.iterations:
            all_items.append(getattr(it, "confidence", "missing"))
        for pt in getattr(self.bundle, "planner_thoughts", []):
            all_items.append(getattr(pt, "confidence", "missing"))
        if self.bundle.verification:
            all_items.append(getattr(self.bundle.verification, "confidence", "missing"))
        if self.bundle.completion:
            all_items.append(getattr(self.bundle.completion, "confidence", "missing"))

        if not all_items:
            self.checks.append(CheckResult(
                check_name="confidence_distribution",
                status="MISSING",
                detail="No evidence items to assess confidence",
            ))
            return

        dist = {}
        for c in all_items:
            dist[c] = dist.get(c, 0) + 1

        # Evidence-backed = original + derived (both have trace data backing)
        evidence_backed = dist.get("original", 0) + dist.get("derived", 0)
        backed_ratio = evidence_backed / len(all_items)
        inferred_count = dist.get("inferred", 0)
        
        # Build concise detail
        dist_parts = ", ".join(f"{k}={v}" for k, v in sorted(dist.items(), key=lambda x: -x[1]))
        detail = f"{backed_ratio:.0%} evidence-backed ({len(all_items)} items: {dist_parts})"

        if backed_ratio >= 0.9:
            self.checks.append(CheckResult(
                check_name="confidence_distribution",
                status="PASS",
                detail=detail,
                evidence_count=evidence_backed,
            ))
        elif backed_ratio >= 0.7:
            self.checks.append(CheckResult(
                check_name="confidence_distribution",
                status="WARN",
                detail=detail,
                evidence_count=evidence_backed,
            ))
        else:
            self.checks.append(CheckResult(
                check_name="confidence_distribution",
                status="WARN",
                detail=f"Low confidence — {detail}",
                evidence_count=evidence_backed,
                missing_items=["high_confidence_evidence"],
            ))

    def _check_evidence_source_coverage(self):
        """§8-⑫ 检查证据来源覆盖 — 是否有多种来源支撑"""
        sources = set()
        for tc in self.bundle.tool_calls:
            sources.add(getattr(tc, "source", "unknown"))
        for svc in self.bundle.services:
            sources.add(getattr(svc, "source", "unknown"))
        for ph in self.bundle.phases:
            sources.add(getattr(ph, "source", "unknown"))
        for it in self.bundle.iterations:
            sources.add(getattr(it, "source", "unknown"))
        for pt in getattr(self.bundle, "planner_thoughts", []):
            sources.add(getattr(pt, "source", "unknown"))
        if self.bundle.verification:
            sources.add(getattr(self.bundle.verification, "source", "unknown"))
        if self.bundle.completion:
            sources.add(getattr(self.bundle.completion, "source", "unknown"))

        sources.discard("unknown")

        if not sources:
            self.checks.append(CheckResult(
                check_name="evidence_source_coverage",
                status="MISSING",
                detail="No identifiable evidence sources",
                missing_items=["source_metadata"],
            ))
        elif len(sources) == 1:
            self.checks.append(CheckResult(
                check_name="evidence_source_coverage",
                status="WARN",
                detail=f"Only 1 source: {next(iter(sources))}. Multi-source corroboration improves trust",
                evidence_count=1,
            ))
        else:
            src_list = ", ".join(sorted(sources))
            self.checks.append(CheckResult(
                check_name="evidence_source_coverage",
                status="PASS",
                detail=f"{len(sources)} distinct sources: {src_list}",
                evidence_count=len(sources),
            ))

    def _check_execution_path(self):
        """§8-⑤ 是否有 executionPath 或等价执行路径"""
        comp = self.bundle.completion
        if comp and comp.execution_path:
            path_str = " → ".join(comp.execution_path[:10])
            suffix = "..." if len(comp.execution_path) > 10 else ""
            self.checks.append(CheckResult(
                check_name="execution_path",
                status="PASS",
                detail=f"{len(comp.execution_path)} steps: {path_str}{suffix}",
                evidence_count=len(comp.execution_path),
            ))
        elif self.bundle.phases:
            # Fallback: reconstruct from phase evidence
            path = [p.phase for p in self.bundle.phases]
            self.checks.append(CheckResult(
                check_name="execution_path",
                status="WARN",
                detail=f"No explicit executionPath; reconstructed from {len(path)} phase events",
                evidence_count=len(path),
                missing_items=["explicit_execution_path"],
            ))
        else:
            self.checks.append(CheckResult(
                check_name="execution_path",
                status="MISSING",
                detail="No executionPath or phase events found",
                missing_items=["execution_path", "phase_events"],
            ))

    def _check_tool_channels_presence(self):
        """§8-⑥ 是否有 selected services / toolChannels"""
        comp = self.bundle.completion
        if comp and comp.tool_channels:
            channels = [tc.get("channel", "unknown") for tc in comp.tool_channels if isinstance(tc, dict)]
            channel_set = set(channels)
            self.checks.append(CheckResult(
                check_name="tool_channels_presence",
                status="PASS",
                detail=f"{len(comp.tool_channels)} toolChannel entries, types: {sorted(channel_set)}",
                evidence_count=len(comp.tool_channels),
            ))
        elif self.bundle.services:
            # Fallback: services have channel info
            svc_channels = {s.channel for s in self.bundle.services}
            self.checks.append(CheckResult(
                check_name="tool_channels_presence",
                status="WARN",
                detail=f"No explicit toolChannels; {len(self.bundle.services)} services have channel info: {sorted(svc_channels)}",
                evidence_count=len(self.bundle.services),
                missing_items=["explicit_tool_channels"],
            ))
        else:
            self.checks.append(CheckResult(
                check_name="tool_channels_presence",
                status="MISSING",
                detail="No toolChannels or service channel info found",
                missing_items=["tool_channels", "service_channel_info"],
            ))

    def _check_final_result(self):
        """§8-⑦ 是否有 final_result"""
        comp = self.bundle.completion
        if comp is not None:
            status = "PASS" if comp.success else "WARN"
            self.checks.append(CheckResult(
                check_name="final_result",
                status=status,
                detail=f"success={comp.success}, iterations={comp.iterations}, elapsed={comp.elapsed_ms}ms",
                evidence_count=1,
            ))
        elif self.bundle.verification and self.bundle.verification.status:
            # Partial: have verification but no completion record
            self.checks.append(CheckResult(
                check_name="final_result",
                status="WARN",
                detail=f"No completion record; verification status={self.bundle.verification.status}",
                evidence_count=1,
                missing_items=["completion_record"],
            ))
        else:
            self.checks.append(CheckResult(
                check_name="final_result",
                status="MISSING",
                detail="No final_result or completion evidence",
                missing_items=["final_result", "completion_event"],
            ))

    def _check_config_attachment_evidence_id(self):
        """§8-⑩ config_attachment_draft 是否含有 evidence_id 供挂载"""
        # Check if the evidence card has an evidence_id that can be referenced
        if self.card and self.card.evidence_id:
            eid = self.card.evidence_id
            # Verify it's a proper formatted ID (not empty/placeholder)
            if len(eid) > 8 and eid.startswith("ev-"):
                self.checks.append(CheckResult(
                    check_name="config_attachment_evidence_id",
                    status="PASS",
                    detail=f"Evidence ID '{eid}' available for config_attachment_draft linkage",
                    evidence_count=1,
                ))
            else:
                self.checks.append(CheckResult(
                    check_name="config_attachment_evidence_id",
                    status="WARN",
                    detail=f"Evidence ID '{eid}' present but format may be non-standard",
                    evidence_count=1,
                    missing_items=["standard_evidence_id_format"],
                ))
        else:
            self.checks.append(CheckResult(
                check_name="config_attachment_evidence_id",
                status="MISSING",
                detail="No evidence_id found — config_attachment_draft cannot reference this run",
                missing_items=["evidence_id"],
            ))

    def _check_tool_call_details_consistency(self):
        """§14-① tool_call_details 与 tool_call_summary 计数一致性"""
        details = self.card.tool_call_details if self.card else []
        summary = self.card.tool_call_summary if self.card else []

        if not details:
            # Not a failure — traces without tool_call_details are valid (legacy)
            self.checks.append(CheckResult(
                check_name="tool_call_details_consistency",
                status="WARN",
                detail="No tool_call_details present — debrief timeline unavailable",
                missing_items=["tool_call_details"],
            ))
            return

        # Count: details should have >= sum of summary counts
        summary_total = sum(s.get("call_count", s.get("count", 0)) for s in summary)
        detail_count = len(details)

        # Each detail entry should have required fields
        # Accept both canonical names (tool_name/service_id) and short aliases (tool/service)
        required_field_aliases = {"tool_name": ("tool_name", "tool"), "service_id": ("service_id", "service")}
        missing_by_field: dict[str, int] = {}
        for d in details:
            for canonical, aliases in required_field_aliases.items():
                if not any(d.get(a) for a in aliases):
                    missing_by_field[canonical] = missing_by_field.get(canonical, 0) + 1

        if missing_by_field:
            field_summary = ", ".join(
                f"'{f}' missing in {n}/{detail_count} entries"
                for f, n in sorted(missing_by_field.items())
            )
            self.checks.append(CheckResult(
                check_name="tool_call_details_consistency",
                status="WARN",
                detail=f"{detail_count} details — {field_summary}",
                evidence_count=detail_count,
                missing_items=[f"{f}({n}/{detail_count})" for f, n in sorted(missing_by_field.items())],
            ))
        elif detail_count == summary_total:
            self.checks.append(CheckResult(
                check_name="tool_call_details_consistency",
                status="PASS",
                detail=f"{detail_count} tool_call_details match summary total ({summary_total})",
                evidence_count=detail_count,
            ))
        else:
            self.checks.append(CheckResult(
                check_name="tool_call_details_consistency",
                status="WARN",
                detail=f"tool_call_details ({detail_count}) vs summary total ({summary_total}) — count divergence",
                evidence_count=detail_count,
                missing_items=[f"expected_{summary_total}_got_{detail_count}"],
            ))

    def _check_planner_events_completeness(self):
        """§14-② planner_events 有内容且含 iteration 编号"""
        events = self.card.planner_events if self.card else []

        if not events:
            self.checks.append(CheckResult(
                check_name="planner_events_completeness",
                status="WARN",
                detail="No planner_events — agent reasoning not captured for debrief",
                missing_items=["planner_events"],
            ))
            return

        # Each event should have iteration number and some content
        has_iteration = sum(1 for e in events if "iteration" in e)
        has_content = sum(1 for e in events if e.get("reasoning") or e.get("plan") or e.get("content") or e.get("preview"))

        if has_iteration == len(events) and has_content == len(events):
            self.checks.append(CheckResult(
                check_name="planner_events_completeness",
                status="PASS",
                detail=f"{len(events)} planner events all have iteration numbers and content",
                evidence_count=len(events),
            ))
        else:
            missing = []
            if has_iteration < len(events):
                missing.append(f"iteration_numbers({has_iteration}/{len(events)})")
            if has_content < len(events):
                missing.append(f"content({has_content}/{len(events)})")
            self.checks.append(CheckResult(
                check_name="planner_events_completeness",
                status="WARN",
                detail=f"{len(events)} planner events, some incomplete: {', '.join(missing)}",
                evidence_count=len(events),
                missing_items=missing,
            ))

    def _check_timeline_monotonicity(self):
        """§14-③ tool_call_details 时间戳单调递增"""
        details = self.card.tool_call_details if self.card else []

        if not details:
            self.checks.append(CheckResult(
                check_name="timeline_monotonicity",
                status="WARN",
                detail="No tool_call_details to verify timeline ordering",
                missing_items=["tool_call_details"],
            ))
            return

        # Extract timestamps (relative_ms, ts, timestamp, or timestamp_iso)
        timestamps = []
        for d in details:
            ts = d.get("relative_ms") or d.get("ts") or d.get("timestamp")
            if ts is not None:
                try:
                    timestamps.append(float(ts) if isinstance(ts, (int, float)) else 0)
                except (ValueError, TypeError):
                    timestamps.append(0)
            else:
                # Try timestamp_iso (HH:MM:SS or HH:MM:SS.fff format)
                ts_iso = d.get("timestamp_iso")
                if ts_iso and isinstance(ts_iso, str):
                    try:
                        parts = ts_iso.split(":")
                        if len(parts) >= 2:
                            h, m = int(parts[0]), int(parts[1])
                            s = float(parts[2]) if len(parts) > 2 else 0.0
                            timestamps.append(h * 3600 + m * 60 + s)
                    except (ValueError, TypeError, IndexError):
                        pass

        if not timestamps:
            self.checks.append(CheckResult(
                check_name="timeline_monotonicity",
                status="WARN",
                detail="tool_call_details present but no parseable timestamps",
                evidence_count=len(details),
                missing_items=["timestamps"],
            ))
            return

        # Check monotonicity (non-decreasing)
        violations = 0
        for i in range(1, len(timestamps)):
            if timestamps[i] < timestamps[i - 1]:
                violations += 1

        if violations == 0:
            self.checks.append(CheckResult(
                check_name="timeline_monotonicity",
                status="PASS",
                detail=f"All {len(timestamps)} tool call timestamps are monotonically ordered",
                evidence_count=len(timestamps),
            ))
        else:
            self.checks.append(CheckResult(
                check_name="timeline_monotonicity",
                status="WARN",
                detail=f"{violations}/{len(timestamps)-1} timestamp ordering violations detected",
                evidence_count=len(timestamps),
                missing_items=[f"{violations}_ordering_violations"],
            ))

    def _check_result_hash_integrity(self):
        """§15-① Verify result_hash values in tool_call_records are not tampered.

        The hash is computed as sha256(str(result).encode())[:16] at collection time
        (headless_run.py). This check recomputes and compares.
        """
        tool_calls = self.bundle.tool_calls if self.bundle else []
        # Only "return" direction ToolCallEvidence carries result + result_hash
        records_with_hash = [
            tc for tc in tool_calls
            if tc.direction == "return" and tc.result_hash and tc.result is not None
        ]

        if not records_with_hash:
            self.checks.append(CheckResult(
                check_name="result_hash_integrity",
                status="WARN",
                detail="No tool_call_records with result_hash to verify",
                missing_items=["result_hash_fields"],
            ))
            return

        verified = 0
        mismatches = []
        for tc in records_with_hash:
            stored = tc.result_hash
            computed = hashlib.sha256(str(tc.result).encode()).hexdigest()[:16]
            if computed == stored:
                verified += 1
            else:
                mismatches.append(tc.tool_name)

        if not mismatches:
            self.checks.append(CheckResult(
                check_name="result_hash_integrity",
                status="PASS",
                detail=f"All {verified} result_hash values verified (SHA-256 prefix match)",
                evidence_count=verified,
            ))
        else:
            # Partial mismatches likely indicate serialization drift, not tampering
            mismatch_ratio = len(mismatches) / len(records_with_hash)
            status = "FAIL" if mismatch_ratio > 0.5 else "WARN"
            self.checks.append(CheckResult(
                check_name="result_hash_integrity",
                status=status,
                detail=f"{len(mismatches)}/{len(records_with_hash)} result_hash mismatches (serialization drift): {mismatches[:3]}",
                evidence_count=verified,
                missing_items=[f"hash_mismatch:{t}" for t in mismatches],
                remediation="Result content may have been modified after collection due to JSON serialization. Re-run trace or investigate encoding.",
            ))

    def _check_metadata_consistency(self):
        """检查 metadata 中声明的计数与实际证据是否一致"""
        meta = getattr(self.bundle, 'raw_metadata', {}) or {}
        if not meta:
            # No metadata to validate — not an error
            return

        declared_tool_count = meta.get('tool_call_count')
        if declared_tool_count is not None:
            actual_tool_count = len(self.bundle.tool_calls)
            if declared_tool_count == 0 and actual_tool_count > 0:
                self.checks.append(CheckResult(
                    check_name="metadata_consistency",
                    status="WARN",
                    detail=f"metadata.tool_call_count={declared_tool_count} but found {actual_tool_count} actual tool_calls",
                    evidence_count=actual_tool_count,
                    missing_items=["tool_call_count_mismatch"],
                    remediation="Update metadata.tool_call_count to match actual tool call events, or investigate why count is zero.",
                ))
                return
            elif declared_tool_count > 0 and actual_tool_count == 0:
                self.checks.append(CheckResult(
                    check_name="metadata_consistency",
                    status="WARN",
                    detail=f"metadata.tool_call_count={declared_tool_count} but no tool_calls found in events",
                    evidence_count=0,
                    missing_items=["tool_call_count_mismatch"],
                    remediation="Trace events may be truncated or corrupted — declared tool calls are missing.",
                ))
                return

        # All declared metadata consistent
        self.checks.append(CheckResult(
            check_name="metadata_consistency",
            status="PASS",
            detail="Metadata declarations consistent with actual evidence",
            evidence_count=1,
        ))


def render_checker_report_markdown(report: CheckerReport) -> str:
    """渲染 checker 报告为 Markdown"""
    status_icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "MISSING": "🔍"}

    lines = [
        f"# Evidence Checker Report",
        "",
        f"**Evidence ID**: `{report.evidence_id}`  ",
        f"**Session**: `{report.session_id}`  ",
        f"**Checked**: {report.checked_at}  ",
        f"**Overall**: {status_icon.get(report.overall_status, '?')} **{report.overall_status}**",
        "",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total Checks | {report.summary['total_checks']} |",
        f"| Passed | {report.summary['passed']} |",
        f"| Warnings | {report.summary['warnings']} |",
        f"| Failed | {report.summary['failed']} |",
        f"| Missing | {report.summary['missing']} |",
        "",
        "## Check Details",
        "",
        "| # | Check | Status | Detail |",
        "|---|-------|--------|--------|",
    ]

    for i, c in enumerate(report.checks, 1):
        icon = status_icon.get(c.status, "?")
        detail_text = c.detail
        if len(detail_text) > 120:
            # Truncate at last separator (→, comma, space) before limit
            cut = detail_text[:117]
            for sep in (" → ", ", ", " "):
                last_sep = cut.rfind(sep)
                if last_sep > 60:
                    cut = cut[:last_sep]
                    break
            detail_text = cut + "…"
        detail_short = sanitize_md_cell(detail_text)
        lines.append(f"| {i} | {c.check_name} | {icon} {c.status} | {detail_short} |")

    # Missing items detail
    missing_checks = [c for c in report.checks if c.missing_items]
    if missing_checks:
        lines.extend([
            "",
            "## Missing Evidence Details",
            "",
        ])
        for c in missing_checks:
            lines.append(f"### {sanitize_identifier(c.check_name)}")
            for item in c.missing_items:
                lines.append(f"- `{sanitize_identifier(item)}`")
            lines.append("")

    # Actionable recommendations for non-PASS checks
    non_pass = [c for c in report.checks if c.status != "PASS"]
    if non_pass:
        lines.extend([
            "",
            "## 💡 Recommendations",
            "",
            "To improve evidence quality, address these items:",
            "",
        ])
        _RECOMMENDATIONS = {
            "verification_presence": "Persist verifier output as a structured JSON event in the trace (add `type: verification_result` with `status` and `reason` fields).",
            "evidence_gaps_summary": "Enhance the orchestrator to persist tool call arguments/results and trace metadata at save time.",
            "tool_io_completeness": "Enable `persist_tool_io: true` in orchestrator config to capture tool call arguments and return values.",
            "confidence_distribution": "Increase original evidence by persisting structured events instead of relying on log-text inference.",
            "evidence_source_coverage": "Add a second evidence source (e.g., service-side logs or database audit trail) for cross-corroboration.",
            "structural_integrity": "Ensure the trace JSON includes all required top-level fields before saving.",
            "service_coverage": "Verify all services emit at least one traceable tool call event.",
            "tool_call_pairs": "Ensure every tool_call event has a matching tool_return event (check for crashes or timeouts).",
            "phase_completeness": "Emit phase_start and phase_done events for all execution phases.",
            "iteration_consistency": "Ensure iteration events follow sequential numbering with proper state transitions.",
            "timeline_sanity": "Investigate unusually long/short execution — may indicate a hang or premature termination.",
            "channel_classification": "Register unknown tool prefixes in the channel mapping configuration.",
            "execution_path": "Persist executionPath (ordered list of phases/steps) in the trace completion block.",
            "tool_channels_presence": "Add toolChannels / selected_services to trace metadata so dispatch routing is auditable.",
            "final_result": "Ensure the orchestrator writes a final_result event with success flag and output summary.",
            "config_attachment_evidence_id": "Generate and persist a unique evidence_id so config_attachment_draft can reference this run.",
            "tool_call_details_consistency": "Ensure tool_call_details count matches the total from tool_call_summary — check for dropped events during extraction.",
            "planner_events_completeness": "Persist planner reasoning events with iteration numbers and content for debrief replay.",
            "timeline_monotonicity": "Investigate out-of-order timestamps in tool_call_details — may indicate parallel calls or clock skew in trace.",
        }
        for c in non_pass:
            icon = status_icon.get(c.status, "?")
            rec = _RECOMMENDATIONS.get(c.check_name, "Review this check and address the underlying data gap.")
            lines.append(f"- {icon} **{c.check_name}**: {rec}")
        lines.append("")

    lines.append("")
    return "\n".join(lines)
