"""Wake#11: Edge-case resilience tests (stdlib unittest).

Tests that the full pipeline (adapter → card → checker → config_attachment)
handles degenerate inputs gracefully without crashing.
"""
import unittest
from dataclasses import dataclass, field

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


def make_tool_call(tool_name="noop", service_id="svc-1", direction="call",
                   timestamp=1717405200.0, **kwargs):
    return ToolCallEvidence(
        tool_name=tool_name,
        service_id=service_id,
        timestamp=timestamp,
        direction=direction,
        **kwargs,
    )


def make_service(service_id="svc-1", **kwargs):
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
    defaults = dict(
        execution_path=["plan", "exec", "verify"],
        tool_channels=[{"channel": "sse", "service": "svc-1"}],
        timestamp=1717405205.0,
    )
    defaults.update(kwargs)
    return CompleteEvidence(
        success=success,
        iterations=iterations,
        elapsed_ms=elapsed_ms,
        **defaults,
    )


def make_verification(status="passed", **kwargs):
    defaults = dict(reason="all checks green", raw_text="raw", missing_evidence=[])
    defaults.update(kwargs)
    return VerificationEvidence(status=status, **defaults)


def make_phase(phase="execution", **kwargs):
    defaults = dict(status="completed", timestamp=1717405200.0)
    defaults.update(kwargs)
    return PhaseEvidence(phase=phase, **defaults)


def make_iteration(iteration=1, **kwargs):
    defaults = dict(status="completed", timestamp=1717405201.0)
    defaults.update(kwargs)
    return IterationEvidence(iteration=iteration, **defaults)


def make_planner_thought(iteration=1, **kwargs):
    defaults = dict(
        content="thinking about next step",
        timestamp=1717405201.0,
        candidate_tools=["tool_a"],
        selected_tools=["tool_a"],
    )
    defaults.update(kwargs)
    return PlannerThoughtEvidence(iteration=iteration, **defaults)


def make_bundle(**kwargs):
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


@dataclass
class FakeCard:
    evidence_id: str = "ev-edge-abc12345"
    verification: dict = field(default_factory=lambda: {"status": "passed"})
    summary: dict = field(default_factory=lambda: {"total_tool_calls": 0})


class TestCheckerEdgeCases(unittest.TestCase):
    """Checker should not crash on degenerate inputs."""

    def _run_checker(self, bundle):
        from trace_evidence.evidence_card import build_evidence_card
        from trace_evidence.evidence_checker import EvidenceChecker
        card = build_evidence_card(bundle)
        checker = EvidenceChecker(bundle, card)
        return checker.run_all()

    def test_completely_empty_bundle(self):
        report = self._run_checker(make_bundle())
        self.assertIn(report.overall_status, ("WARN", "FAIL", "WARN_INCOMPLETE", "INCOMPLETE_TRACE"))
        self.assertGreater(report.summary["total_checks"], 0)

    def test_single_tool_call_no_return(self):
        bundle = make_bundle(
            tool_calls=[make_tool_call(tool_name="search", direction="call")],
            services=[make_service()],
            completion=make_completion(iterations=1),
        )
        report = self._run_checker(bundle)
        self.assertIn(report.overall_status, ("WARN", "FAIL", "WARN_INCOMPLETE", "INCOMPLETE_TRACE"))
        pair_check = [c for c in report.checks if c.check_name == "tool_call_pairs"]
        self.assertEqual(len(pair_check), 1)

    def test_single_tool_call_with_return(self):
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
        self.assertIn(report.overall_status, ("PASS", "WARN", "WARN_INCOMPLETE", "INCOMPLETE_TRACE"))

    def test_no_planner_thoughts(self):
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="calc", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="calc", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(iterations=1),
            verification=make_verification(),
            planner_thoughts=[],
            phases=[make_phase()],
            iterations=[make_iteration()],
        )
        report = self._run_checker(bundle)
        self.assertGreater(report.summary["total_checks"], 0)

    def test_no_completion(self):
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=None,
            verification=make_verification(),
        )
        report = self._run_checker(bundle)
        self.assertIn(report.overall_status, ("WARN", "FAIL", "WARN_INCOMPLETE", "INCOMPLETE_TRACE"))
        self.assertGreater(report.summary["total_checks"], 0)

    def test_no_verification(self):
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=make_completion(),
            verification=None,
        )
        report = self._run_checker(bundle)
        self.assertIn(report.overall_status, ("WARN", "FAIL", "WARN_INCOMPLETE", "INCOMPLETE_TRACE"))

    def test_many_iterations_single_tool(self):
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
        self.assertGreater(report.summary["total_checks"], 0)


class TestExecutionEvidenceSlot(unittest.TestCase):
    """Validates executionEvidence structure in config_attachment_draft."""

    def _build_draft(self, bundle, card=None):
        from trace_evidence.config_attachment import build_config_attachment_draft
        from trace_evidence.evidence_card import build_evidence_card
        if card is None:
            card = build_evidence_card(bundle)
        return build_config_attachment_draft(bundle, card)

    def test_execution_evidence_present(self):
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="search", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="search", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(),
        )
        d = self._build_draft(bundle).to_dict()
        self.assertIn("execution_evidence", d)

    def test_execution_evidence_structure(self):
        bundle = make_bundle(
            tool_calls=[
                make_tool_call(tool_name="search", direction="call", timestamp=1717405200.0),
                make_tool_call(tool_name="search", direction="return", timestamp=1717405201.0),
            ],
            services=[make_service()],
            completion=make_completion(),
        )
        ee = self._build_draft(bundle).to_dict()["execution_evidence"]
        self.assertIsInstance(ee, dict)
        self.assertIn("traceSessionId", ee)
        self.assertIn("evidenceId", ee)
        self.assertIn("verdict", ee)
        self.assertIn("dispatchSequence", ee)
        self.assertIn("metrics", ee)

    def test_execution_evidence_empty_bundle(self):
        ee = self._build_draft(make_bundle()).to_dict()["execution_evidence"]
        self.assertEqual(ee["executionPath"], [])
        self.assertEqual(ee["toolChannels"], [])
        self.assertEqual(ee["dispatchSequence"], [])

    def test_execution_evidence_multiple_services(self):
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
        ee = self._build_draft(bundle).to_dict()["execution_evidence"]
        self.assertGreaterEqual(len(ee["dispatchSequence"]), 1)

    def test_execution_evidence_has_duration(self):
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=make_completion(elapsed_ms=12345),
        )
        ee = self._build_draft(bundle).to_dict()["execution_evidence"]
        self.assertEqual(ee["metrics"]["elapsed_ms"], 12345)


class TestMarkdownReadability(unittest.TestCase):
    """Markdown output structure and collapsible sections."""

    def _render(self, bundle):
        from trace_evidence.evidence_card import build_evidence_card, render_evidence_card_markdown
        card = build_evidence_card(bundle)
        return render_evidence_card_markdown(card)

    def test_markdown_has_header(self):
        bundle = make_bundle(
            tool_calls=[make_tool_call()],
            services=[make_service()],
            completion=make_completion(),
        )
        self.assertTrue(self._render(bundle).startswith("# Evidence Card:"))

    def test_markdown_collapsible_tool_timeline(self):
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
        self.assertIn("<details>", md)
        self.assertIn("<summary>", md)
        self.assertIn("</details>", md)

    def test_markdown_small_timeline_no_collapsible(self):
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
        self.assertNotIn("<details>", self._render(bundle))


if __name__ == "__main__":
    unittest.main()
