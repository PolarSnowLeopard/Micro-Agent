"""
test_graceful_degradation.py — Regression tests ensuring the pipeline
never returns PASS for corrupted / truncated / empty traces.

Guards against false-positive vulnerabilities where a crafted minimal
trace could sneak through all checks.
"""
import sys
import json
import copy
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trace_adapter import TraceEvidenceAdapter
from evidence_card import build_evidence_card
from evidence_checker import EvidenceChecker


def _load_fixture_trace():
    path = Path(__file__).parent / "fixtures" / "minimal_v1_trace.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _run_checker(trace_data):
    """Adapter → Card → Checker pipeline, returns report."""
    adapter = TraceEvidenceAdapter(trace_data)
    bundle = adapter.extract()
    card = build_evidence_card(bundle)
    checker = EvidenceChecker(bundle, card)
    report = checker.run_all()
    return {'overall_status': report.overall_status, 'checks': report.checks}


class TestGracefulDegradation(unittest.TestCase):
    """Ensure corrupted inputs degrade to WARN/FAIL, never PASS."""

    @classmethod
    def setUpClass(cls):
        cls.real_trace = _load_fixture_trace()
        if cls.real_trace is None:
            raise unittest.SkipTest("minimal_v1_trace fixture not available")

    def _assert_not_pass(self, trace_data, msg):
        """Run checker and assert overall_status != PASS."""
        report = _run_checker(trace_data)
        self.assertNotEqual(report['overall_status'], 'PASS', msg)
        return report

    def test_truncated_3_events(self):
        """A trace truncated to 3 events must not get PASS."""
        trace = copy.deepcopy(self.real_trace)
        trace['events'] = trace['events'][:3]
        self._assert_not_pass(trace, "Truncated trace (3 events) must not PASS")

    def test_empty_events(self):
        """A trace with zero events must not get PASS."""
        trace = copy.deepcopy(self.real_trace)
        trace['events'] = []
        self._assert_not_pass(trace, "Empty events trace must not PASS")

    def test_null_metadata_rejected(self):
        """Null metadata is rejected at adapter init (v1 required)."""
        trace = copy.deepcopy(self.real_trace)
        trace['metadata'] = None
        with self.assertRaises(ValueError):
            TraceEvidenceAdapter(trace)

    def test_missing_session_id(self):
        """A trace with missing session_id must not get PASS."""
        trace = copy.deepcopy(self.real_trace)
        trace.pop('session_id', None)
        self._assert_not_pass(trace, "Missing session_id must not PASS")

    def test_events_not_a_list(self):
        """Non-list events should not crash or PASS."""
        trace = copy.deepcopy(self.real_trace)
        trace['events'] = "not_a_list"
        try:
            report = _run_checker(trace)
            self.assertNotEqual(report['overall_status'], 'PASS',
                                "String events must not PASS")
        except (TypeError, AttributeError, ValueError):
            pass  # Raising is acceptable — no false PASS

    def test_tool_call_count_mismatch(self):
        """If metadata.tool_call_count=0 but events have tool_calls, should WARN."""
        trace = copy.deepcopy(self.real_trace)
        if trace.get('metadata') and isinstance(trace['metadata'], dict):
            trace['metadata']['tool_call_count'] = 0
            report = _run_checker(trace)
            self.assertIn(report['overall_status'], ['WARN', 'FAIL'],
                          "Inconsistent tool_call_count=0 should trigger WARN/FAIL")

    def test_single_event_only(self):
        """A trace with just 1 event must not get PASS."""
        trace = copy.deepcopy(self.real_trace)
        trace['events'] = trace['events'][:1]
        self._assert_not_pass(trace, "Single event trace must not PASS")


if __name__ == '__main__':
    unittest.main()
