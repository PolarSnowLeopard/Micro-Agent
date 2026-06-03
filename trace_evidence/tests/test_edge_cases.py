"""Wake#11: Edge-case resilience tests.

Tests that the full pipeline (adapter → card → checker → config_attachment)
handles degenerate inputs gracefully without crashing:
- Empty bundle (no tool calls, no services, no planner)
- Single iteration, single tool call
- Missing completion
- Missing verification
- executionEvidence slot structure validation
"""
import pytest
from dataclasses import dataclass, field
from typing import Optional

# Import real dataclasses from trace_adapter to avoid interface mismatches
from trace_evidence.trace_adapter import (
    ToolCallEvidence,
    ServiceEvidence,
    CompleteEvidence,
    VerificationEvidence,
    PlannerThoughtEvidence,
    PhaseEvidence,
    IterationEvidence,
    TraceEvidenceBundle,
)


# --- Helper factory functions ---

def make_tool_call(tool_name="noop", service_id="svc-1", direction="call",
                   timestamp=1717405200.0, **kwargs):
    """Create a ToolCallEvidence with sensible defaults."""
    return ToolCallEvidence(
        tool_name=tool_name,
        service_id=service_id,
        timestamp=timestamp,
        direction=direction,
        **kwargs,
    )


def make_service(service_id="svc-1", **kwargs):
    """Create a ServiceEvidence with sensible defaults."""
    defaults = dict(
        status="connected",
        channel="sse",
        tools=["tool_a"],
        latency_ms=50,
        timestamp=1717405200.0,
    )
    defaults.update(kwargs)
    return ServiceEvidence(service_id=service_id, **defaults)


def make_completion(success=True, iterations=3, elapsed_ms=5000, **kwargs):
    """Create a CompleteEvidence with sensible defaults."""
    defaults = dict(
        execution_path=["plan", "exec", "verify"],
        tool_channels=[{"channel": "sse", "service": "svc-1"}],
        timestamp=1717405205.0,
    )
    defaults.update(kwargs)
    return CompleteEvidence(success=success, iterations=iterations,
                             elapsed_ms=elapsed_ms, **defaults)


def make_verification(status="passed", **kwargs):
    """Create a VerificationEvidence with sensible defaults."""
    defaults = dict(reason="all checks green", raw_text="raw", missing_evidence=[])
    defaults.update(kwargs)
    return VerificationEvidence(status=status, **defaults)


def make_phase(phase="execution", **kwargs):
    """Create a PhaseEvidence with sensible defaults."""
    defaults = dict(status="completed", timestamp=1717405200.0)
    defaults.update(kwargs)
    return PhaseEvidence(phase=phase, **defaults)


def make_iteration(iteration=1, **kwargs):
    """Create an IterationEvidence with sensible defaults."""
    defaults = dict(status="completed", timestamp=1717405201.0)
    defaults.update(kwargs)
    return IterationEvidence(iteration=iteration, **defaults)


def make_planner_thought(iteration=1, **kwargs):
    """Create a PlannerThoughtEvidence with sensible defaults."""
    defaults = dict(
        content="thinking about next step",
        timestamp=1717405201.0,
        candidate_tools=["tool_a"],
        selected_tools=["tool_a"],
    )
    defaults.update(kwargs)
    return PlannerThoughtEvidence(iteration=iteration, **defaults)


def make_bundle(**kwargs):
    """Create a TraceEvidenceBundle with sensible defaults."""
    defaults = dict(
        session_id="test-session-edge",
        app_name="test-app",
        domain="testing",
        mode="headless",
        strategy={"type": "sequential"},
        services=[],
        tool_calls=[],
        planner_thoughts=[],
        phases=[],
        iterations=[],
        completion=None,
        verification=None,
        missing_evidence=[],
    )
    defaults.update(kwargs)
    return TraceEvidenceBundle(**defaults)


# --- Fake Card for config_attachment tests ---
@dataclass
class FakeCard:
    evidence_id: str = "ev-edge-abc12345"
    verification: dict = field(default_factory=lambda: {"status": "passed"})
    summary: dict = field(default_factory=lambda: {"total_tool_calls": 0})


# ============================================================================
# TEST CLASS: Checker Edge Cases
# ============================================================================

class TestCheckerEdgeCases:
    """Checker should not crash on degenerate inputs."""

    def _run_checker(self, bundle):
        """Build card + run checker on bundle."""
        from trace_evidence.evidence_card import build_evidence_card
        from trace_evidence.evidence_checker import EvidenceChecker
        card = build_evidence_card(bundle)
        checker = EvidenceChecker(bundle, card)
        report = checker.run_all()
        return report

    def test_completely_empty_bundle(self):
        """Zero tool calls, no services, no planner, no completion, no verification."""
        bundle = make_bundle()
        report = self._run_checker(bundle)
        # Should not crash; overall should be WARN or FAIL (not crash)
        assert report.overall_status in ("WARN", "FAIL")
        assert report.summary["total_checks"] > 0

    def test_single_tool_call_no_return(self):
        """One call event, no matching return — checker should flag it."""
        bundle = make_bundle(
            tool_calls=[make_tool_call(tool_name="search", direction="call")],
            services=[make_service()],
            completion=make_completion(iterations=1),
        )
        report = self._run_checker(bundle)
        assert report.overall_status in ("WARN", "FAIL")
        # tool_call_pairs check should notice the mismatch
        pair_check = [c for c in report.checks if c.check_name == "tool_call_pairs"]
        assert len(pair_check) == 1

    def test_single_tool_call_with_return(self):
        """One call + one return — minimal valid trace."""
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="search", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="search", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(iterations=1),
            verification=make_verification(),
            phases=[make_phase()],
            iterations=[make_iteration()],
        )
        report = self._run_checker(bundle)
        # Minimal valid trace should get PASS or WARN (not FAIL)
        assert report.overall_status in ("PASS", "WARN")

    def test_no_planner_thoughts(self):
        """Bundle with tool calls but zero planner thoughts — should still work."""
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="calc", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="calc", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(iterations=1),
            verification=make_verification(),
            planner_thoughts=[],  # explicitly empty
            phases=[make_phase()],
            iterations=[make_iteration()],
        )
        report = self._run_checker(bundle)
        # Should not crash
        assert report.summary["total_checks"] > 0

    def test_no_completion(self):
        """Missing completion should not crash checker."""
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=None,
            verification=make_verification(),
        )
        report = self._run_checker(bundle)
        assert report.overall_status in ("WARN", "FAIL")
        assert report.summary["total_checks"] > 0

    def test_no_verification(self):
        """Missing verification should not crash checker."""
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=make_completion(),
            verification=None,
        )
        report = self._run_checker(bundle)
        assert report.overall_status in ("WARN", "FAIL")

    def test_many_iterations_single_tool(self):
        """High iteration count but only 1 tool call — edge case for ratio checks."""
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="api_call", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="api_call", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(iterations=50, elapsed_ms=100000),
            verification=make_verification(),
            phases=[make_phase()],
            iterations=[make_iteration(iteration=i) for i in range(50)],
        )
        report = self._run_checker(bundle)
        assert report.summary["total_checks"] > 0


# ============================================================================
# TEST CLASS: executionEvidence Slot in ConfigAttachment
# ============================================================================

class TestExecutionEvidenceSlot:
    """Validates executionEvidence structure in config_attachment_draft."""

    def _build_draft(self, bundle, card=None):
        from trace_evidence.config_attachment import build_config_attachment_draft
        from trace_evidence.evidence_card import build_evidence_card
        if card is None:
            card = build_evidence_card(bundle)
        return build_config_attachment_draft(bundle, card)

    def test_execution_evidence_present(self):
        """executionEvidence slot should exist in draft output."""
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="search", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="search", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(),
        )
        draft = self._build_draft(bundle)
        d = draft.to_dict()
        assert "execution_evidence" in d

    def test_execution_evidence_structure(self):
        """executionEvidence should contain required keys from config_attachment."""
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="search", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="search", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(),
        )
        draft = self._build_draft(bundle)
        d = draft.to_dict()
        ee = d["execution_evidence"]
        assert isinstance(ee, dict)
        assert "traceSessionId" in ee
        assert "evidenceId" in ee
        assert "verdict" in ee
        assert "dispatchSequence" in ee
        assert "metrics" in ee

    def test_execution_evidence_empty_bundle(self):
        """executionEvidence with empty bundle should have empty paths."""
        bundle = make_bundle()
        draft = self._build_draft(bundle)
        d = draft.to_dict()
        ee = d["execution_evidence"]
        assert ee["executionPath"] == []
        assert ee["toolChannels"] == []
        assert ee["dispatchSequence"] == []

    def test_execution_evidence_multiple_services(self):
        """executionEvidence.dispatchSequence lists dispatched tools."""
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="a", service_id="svc-1", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="b", service_id="svc-2", direction="call", timestamp=1717405201.0),
                make_tool_call(tool_name="a", service_id="svc-1", direction="return", timestamp=1717405202.0),
                make_tool_call(tool_name="b", service_id="svc-2", direction="return", timestamp=1717405203.0),
            ],
            services=[make_service(service_id="svc-1"), make_service(service_id="svc-2")],
            completion=make_completion(),
        )
        draft = self._build_draft(bundle)
        d = draft.to_dict()
        ee = d["execution_evidence"]
        # dispatchSequence should have entries for tool calls
        assert len(ee["dispatchSequence"]) >= 1

    def test_execution_evidence_has_duration(self):
        """executionEvidence.metrics should contain elapsed_ms from completion."""
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=make_completion(elapsed_ms=12345),
        )
        draft = self._build_draft(bundle)
        d = draft.to_dict()
        ee = d["execution_evidence"]
        assert ee["metrics"]["elapsed_ms"] == 12345


# ============================================================================
# TEST CLASS: Evidence Card Markdown Readability
# ============================================================================

class TestMarkdownReadability:
    """Tests that markdown output is well-structured and uses collapsible sections."""

    def _render(self, bundle):
        from trace_evidence.evidence_card import build_evidence_card, render_evidence_card_markdown
        card = build_evidence_card(bundle)
        return render_evidence_card_markdown(card)

    def test_markdown_has_header(self):
        """Output starts with # Evidence Card."""
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=make_completion(),
        )
        md = self._render(bundle)
        assert md.startswith("# Evidence Card:")

    def test_markdown_collapsible_tool_timeline(self):
        """When >5 tool calls, timeline should be in a collapsible <details> block."""
        calls = []
        for i in range(10):
            calls.append(make_tool_call(
                tool_name=f"tool_{i}", direction="call",
                timestamp=1717405200.0 + i * 2,
            ))
            calls.append(make_tool_call(
                tool_name=f"tool_{i}", direction="return",
                timestamp=1717405200.0 + i * 2 + 1,
            ))
        bundle = make_bundle(
            tool_calls=calls,
            services=[make_service()],
            completion=make_completion(iterations=10),
            verification=make_verification(),
            phases=[make_phase()],
            iterations=[make_iteration(iteration=i) for i in range(10)],
        )
        md = self._render(bundle)
        assert "<details>" in md
        assert "<summary>" in md
        assert "</details>" in md

    def test_markdown_small_timeline_no_collapsible(self):
        """When <=5 tool calls, no collapsible block needed."""
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="a", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="a", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(iterations=1),
            verification=make_verification(),
            phases=[make_phase()],
            iterations=[make_iteration()],
        )
        md = self._render(bundle)
        # Small timeline should NOT have collapsible
        assert "<details>" not in md
