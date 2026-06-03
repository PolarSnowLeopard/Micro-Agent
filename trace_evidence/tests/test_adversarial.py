"""
Pass #21: Adversarial Input Resilience Tests.

Validates that the pipeline handles malformed, malicious, and unexpected
inputs gracefully — never crashes, never produces misleading output.
"""
import unittest
import tempfile
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from trace_evidence import run_pipeline
from trace_evidence.trace_adapter import TraceEvidenceAdapter


class TestRunPipelineInputValidation(unittest.TestCase):
    """run_pipeline() rejects invalid inputs with clear errors."""

    def test_none_raises_type_error(self):
        with self.assertRaises(TypeError) as cm:
            run_pipeline(None)
        self.assertIn("NoneType", str(cm.exception))

    def test_list_raises_type_error(self):
        with self.assertRaises(TypeError) as cm:
            run_pipeline([1, 2, 3])
        self.assertIn("list", str(cm.exception))

    def test_int_raises_type_error(self):
        with self.assertRaises(TypeError) as cm:
            run_pipeline(42)
        self.assertIn("int", str(cm.exception))

    def test_nonexistent_path_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            run_pipeline("/nonexistent/path/to/trace.json")

    def test_invalid_json_file_raises_json_error(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            path = f.name
        try:
            with self.assertRaises(json.JSONDecodeError):
                run_pipeline(path)
        finally:
            os.unlink(path)


class TestAdapterMalformedEvents(unittest.TestCase):
    """Adapter handles malformed event arrays without crashing."""

    def _make_trace(self, events):
        return {
            "events": events,
            "metadata": {"app_name": "adversarial_test", "simulation_id": "adv-001"},
        }

    def test_events_is_none(self):
        trace = self._make_trace(None)
        trace["events"] = None  # force None
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        self.assertEqual(len(bundle.tool_calls), 0)

    def test_events_is_string(self):
        trace = {"events": "not a list", "metadata": {}}
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        self.assertEqual(len(bundle.tool_calls), 0)

    def test_event_missing_type(self):
        trace = self._make_trace([
            {"timestamp": "2024-01-01T00:00:00Z", "data": {"msg": "no type field"}},
        ])
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        self.assertEqual(len(bundle.tool_calls), 0)
        # Should record diagnostic about dropped events
        self.assertTrue(len(bundle.diagnostics) > 0)
        self.assertEqual(bundle.diagnostics[0]["code"], "events_dropped")

    def test_event_missing_timestamp(self):
        trace = self._make_trace([
            {"type": "tool_call", "data": {"tool_name": "test"}},
        ])
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        self.assertEqual(len(bundle.tool_calls), 0)
        self.assertTrue(any(d["code"] == "events_dropped" for d in bundle.diagnostics))

    def test_event_data_not_dict(self):
        trace = self._make_trace([
            {"type": "tool_call", "timestamp": "2024-01-01T00:00:00Z", "data": "string_data"},
        ])
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        self.assertEqual(len(bundle.tool_calls), 0)

    def test_null_event_in_list(self):
        trace = self._make_trace([None, None, None])
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        self.assertEqual(len(bundle.tool_calls), 0)
        self.assertTrue(len(bundle.diagnostics) > 0)

    def test_mixed_valid_and_invalid_events(self):
        """Valid events still extracted even when mixed with invalid ones."""
        trace = self._make_trace([
            None,
            {"no": "type"},
            {"type": "tool_call_record", "timestamp": 1704067200.0,
             "data": {"tool_name": "read_file", "service_id": "fs",
                      "args": {}, "result": "ok", "channel": "stdio"}},
            "string_event",
        ])
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        # Should extract the one valid tool_call_record event (produces call+return pair)
        self.assertGreaterEqual(len(bundle.tool_calls), 1)
        # Should report 3 dropped events
        dropped_diag = [d for d in bundle.diagnostics if d["code"] == "events_dropped"]
        self.assertEqual(len(dropped_diag), 1)
        self.assertEqual(dropped_diag[0]["dropped_count"], 3)


class TestAdapterXSSResistance(unittest.TestCase):
    """Markdown output doesn't contain raw HTML/script injection."""

    def test_xss_in_tool_name_sanitized(self):
        trace = {
            "events": [
                {"type": "mcp_tool_call", "timestamp": "2024-01-01T00:00:00Z",
                 "data": {"tool_name": "<script>alert('xss')</script>",
                          "service_id": "evil", "args": {}, "result": "pwned"}},
            ],
            "metadata": {"app_name": "<img onerror=alert(1)>", "simulation_id": "xss-test"},
        }
        result = run_pipeline(trace)
        # Card markdown should not contain raw script tags
        self.assertNotIn("<script>", result.card_md)
        self.assertNotIn("onerror=", result.card_md)

    def test_xss_in_args_sanitized(self):
        trace = {
            "events": [
                {"type": "mcp_tool_call", "timestamp": "2024-01-01T00:00:00Z",
                 "data": {"tool_name": "safe_tool", "service_id": "s",
                          "args": {"payload": "<script>document.cookie</script>"},
                          "result": "ok"}},
            ],
            "metadata": {"app_name": "test", "simulation_id": "xss-args"},
        }
        result = run_pipeline(trace)
        self.assertNotIn("<script>", result.card_md)


class TestAdapterLargeInput(unittest.TestCase):
    """Pipeline handles oversized inputs without OOM or excessive output."""

    def test_huge_args_truncated(self):
        """Tool call with 1MB args shouldn't produce 1MB card."""
        big_data = "x" * (1024 * 1024)  # 1MB string
        trace = {
            "events": [
                {"type": "mcp_tool_call", "timestamp": "2024-01-01T00:00:00Z",
                 "data": {"tool_name": "big_tool", "service_id": "s",
                          "args": {"huge": big_data}, "result": big_data}},
            ],
            "metadata": {"app_name": "big", "simulation_id": "big-test"},
        }
        result = run_pipeline(trace)
        # Card should be reasonable size (under 50KB)
        self.assertLess(len(result.card_md), 50 * 1024)

    def test_many_events(self):
        """500 events shouldn't crash or take unreasonable time."""
        events = [
            {"type": "tool_call_record", "timestamp": 1704067200.0 + i,
             "data": {"tool_name": f"tool_{i}", "service_id": "s",
                      "args": {"i": i}, "result": f"result_{i}", "channel": "stdio"}}
            for i in range(500)
        ]
        trace = {"events": events, "app_name": "bulk", "session_id": "bulk-test"}
        result = run_pipeline(trace)
        self.assertGreater(len(result.bundle.tool_calls), 0)
        # Should complete without error
        self.assertIsNotNone(result.card_md)


class TestAdapterUnicode(unittest.TestCase):
    """Unicode and special characters handled cleanly."""

    def test_null_bytes_stripped(self):
        trace = {
            "events": [
                {"type": "mcp_tool_call", "timestamp": "2024-01-01T00:00:00Z",
                 "data": {"tool_name": "unicode_tool\x00", "service_id": "s\x00",
                          "args": {"key": "val\x00ue"}, "result": "ok\x00"}},
            ],
            "metadata": {"app_name": "null\x00byte", "simulation_id": "unicode-test"},
        }
        result = run_pipeline(trace)
        self.assertNotIn("\x00", result.card_md)

    def test_chinese_and_emoji(self):
        trace = {
            "session_id": "cn-test",
            "app_name": "中文应用",
            "events": [
                {"type": "tool_call_record", "timestamp": 1704067200.0,
                 "data": {"tool_name": "搜索工具🔍", "service_id": "搜索服务",
                          "args": {"query": "你好世界🌍"}, "result": "找到了✓", "channel": "stdio"}},
            ],
        }
        result = run_pipeline(trace)
        # Should not crash, card should contain Chinese text
        self.assertIn("中文应用", result.card_md)


if __name__ == "__main__":
    unittest.main()
