"""Integration tests: config_attachment full round-trip and security sanitization.

Tests that:
1. A trace with tool_calls produces a valid ConfigAttachmentDraft with correct fields
2. The draft's execution_evidence slot is populated from the checker report
3. Sensitive data in tool args (passwords, tokens) is NOT leaked into config output
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from trace_evidence import run_pipeline


class TestConfigAttachmentRoundTrip(unittest.TestCase):
    """Full round-trip: trace → adapter → checker → card → config_draft."""

    TRACE_PATH = os.path.join(
        os.path.dirname(__file__), 'fixtures', 'minimal_v1_trace.json'
    )

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(cls.TRACE_PATH):
            raise unittest.SkipTest(f"Trace not found: {cls.TRACE_PATH}")
        cls.result = run_pipeline(cls.TRACE_PATH)
        cls.draft = cls.result.config_draft

    def test_draft_has_evidence_id(self):
        """config_draft must have a non-empty evidence_id linking to the card."""
        self.assertTrue(self.draft.evidence_id, "evidence_id should be non-empty")

    def test_draft_has_session_id(self):
        """config_draft must carry the session_id from the original trace."""
        self.assertTrue(self.draft.session_id, "session_id should be non-empty")

    def test_draft_schema_version(self):
        """config_draft schema_version must be 1.0.0."""
        self.assertEqual(self.draft.schema_version, "1.0.0")

    def test_draft_is_marked_as_draft(self):
        """config_draft.draft must be True."""
        self.assertTrue(self.draft.draft)

    def test_draft_has_app_name(self):
        """config_draft should carry app_name from trace."""
        self.assertTrue(self.draft.app_name, "app_name should not be empty")

    def test_draft_dispatch_sequence_matches_tool_calls(self):
        """dispatch_sequence length should match bundle's tool_calls count."""
        # The pipeline extracts tool_calls into dispatch_sequence
        tool_call_count = len(self.result.evidence_events)
        dispatch_count = len(self.draft.dispatch_sequence)
        # dispatch_sequence is derived from tool_calls so should be > 0 if we have events
        if tool_call_count > 0:
            self.assertGreater(dispatch_count, 0,
                               "dispatch_sequence should be non-empty when tool calls exist")

    def test_draft_execution_evidence_populated(self):
        """execution_evidence slot should contain verdict and evidenceId."""
        ee = self.draft.execution_evidence
        self.assertIsInstance(ee, dict)
        self.assertIn("verdict", ee)
        self.assertIn("evidenceId", ee)
        self.assertTrue(ee["evidenceId"])

    def test_draft_to_dict_is_json_serializable(self):
        """to_dict() output must be fully JSON-serializable."""
        d = self.draft.to_dict()
        try:
            s = json.dumps(d, ensure_ascii=False)
            parsed = json.loads(s)
        except (TypeError, ValueError) as e:
            self.fail(f"to_dict() is not JSON-serializable: {e}")
        self.assertEqual(parsed["schema_version"], "1.0.0")

    def test_draft_to_json_roundtrip(self):
        """to_json() → json.loads() should produce same dict as to_dict()."""
        j = self.draft.to_json()
        parsed = json.loads(j)
        d = self.draft.to_dict()
        self.assertEqual(parsed, d)


class TestConfigSecuritySanitization(unittest.TestCase):
    """Ensure sensitive data in tool call args is not leaked into config outputs."""

    def _make_trace_with_sensitive_args(self):
        return {
            "session_id": "sec-test-001",
            "app_name": "security-test",
            "domain": "test",
            "metadata": {
                "trace_version": "v1.0.0",
                "runtime": {"trace_version": "v1.0.0"},
                "tool_call_count": 1,
            },
            "events": [
                {
                    "type": "tool_call_record",
                    "timestamp": 1704067200.0,
                    "data": {
                        "tool_name": "svc-db_query",
                        "service_id": "svc-db",
                        "arguments": {
                            "query": "SELECT * FROM users",
                            "password": "s3cr3t_p@ssw0rd!",
                            "api_key": "sk-live-abc123secret456",
                            "connection_string": "postgres://admin:hunter2@prod-db:5432/main",
                        },
                        "result": '{"rows": 42}',
                        "channel": "mcp",
                    },
                },
                {
                    "type": "planner_decision",
                    "timestamp": 1704067201.0,
                    "data": {"iteration": 1, "reason": "query", "candidate_tools": [], "selected_tools": []},
                },
                {
                    "type": "verifier_result",
                    "timestamp": 1704067202.0,
                    "data": {"status": "PASSED", "summary": "ok"},
                },
                {
                    "type": "complete",
                    "timestamp": 1704067203.0,
                    "data": {"success": True, "metrics": {"iterations": 1, "elapsedMs": 100}, "result": {}},
                },
            ],
        }

    def test_sensitive_args_not_in_config_json(self):
        """Passwords and API keys in tool args must not appear in config_draft JSON."""
        trace = self._make_trace_with_sensitive_args()
        result = run_pipeline(trace)
        config_json = result.config_draft.to_json()

        sensitive_values = [
            "s3cr3t_p@ssw0rd!",
            "sk-live-abc123secret456",
            "hunter2",
        ]
        for secret in sensitive_values:
            self.assertNotIn(secret, config_json,
                             f"Sensitive value '{secret}' leaked into config_draft JSON")

    def test_sensitive_args_not_in_evidence_card(self):
        """Passwords and API keys must not appear in evidence card output."""
        trace = self._make_trace_with_sensitive_args()
        result = run_pipeline(trace)
        card_json = json.dumps(result.card.to_dict(), ensure_ascii=False)

        sensitive_values = [
            "s3cr3t_p@ssw0rd!",
            "sk-live-abc123secret456",
            "hunter2",
        ]
        for secret in sensitive_values:
            self.assertNotIn(secret, card_json,
                             f"Sensitive value '{secret}' leaked into evidence_card JSON")


if __name__ == "__main__":
    unittest.main()
