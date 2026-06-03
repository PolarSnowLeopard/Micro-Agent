"""
Trace Evidence Infrastructure — Integration Tests
Covers: adapter extraction, evidence card generation, checker, robustness.

Run:  python -m unittest trace_evidence/tests/test_pipeline.py
  or: python trace_evidence/tests/test_pipeline.py
"""
import sys
import json
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trace_adapter import TraceEvidenceAdapter
from evidence_card import build_evidence_card, render_evidence_card_markdown
from evidence_checker import EvidenceChecker


REAL_TRACE = Path(__file__).parent / "fixtures/minimal_v1_trace.json"
V1_META = {"trace_version": "v1.0.0", "runtime": {"trace_version": "v1.0.0"}}


def _load_real_trace():
    """Load real trace or skip if not available."""
    if not REAL_TRACE.exists():
        return None
    return json.loads(REAL_TRACE.read_text())


def _make_bundle(trace_data):
    adapter = TraceEvidenceAdapter(trace_data)
    return adapter.extract()


class TestTraceAdapter(unittest.TestCase):
    """§6: Source/confidence field extraction"""

    @classmethod
    def setUpClass(cls):
        cls.trace = _load_real_trace()
        if cls.trace is None:
            raise unittest.SkipTest("Real trace file not available")
        cls.bundle = _make_bundle(cls.trace)

    def test_extract_returns_bundle(self):
        self.assertNotEqual(self.bundle.session_id, "unknown")
        self.assertGreater(len(self.bundle.tool_calls), 0)

    def test_tool_calls_have_source_and_confidence(self):
        for tc in self.bundle.tool_calls:
            self.assertEqual(tc.source, "persisted_metadata")
            self.assertEqual(tc.confidence, "original")

    def test_services_have_channel(self):
        for s in self.bundle.services:
            self.assertIn(s.channel, ("mcp", "rest", "grpc", "unknown"))

    def test_phases_have_source(self):
        for p in self.bundle.phases:
            self.assertTrue(hasattr(p, "source"))
            self.assertEqual(p.source, "original_trace")


class TestChannelEnrichment(unittest.TestCase):
    """Channel cross-reference enrichment logic"""

    @classmethod
    def setUpClass(cls):
        cls.trace = _load_real_trace()
        if cls.trace is None:
            raise unittest.SkipTest("Real trace file not available")
        cls.bundle = _make_bundle(cls.trace)

    def test_no_unknown_channels_for_known_services(self):
        """Tool calls with known service_id should not have 'unknown' channel."""
        known_sids = {s.service_id for s in self.bundle.services}
        for tc in self.bundle.tool_calls:
            if tc.service_id in known_sids:
                self.assertNotEqual(tc.channel, "unknown",
                                    f"{tc.tool_name} (service={tc.service_id}) still has unknown channel")

    def test_internal_classified_as_local(self):
        """Internal tool calls (terminate) should have 'local' channel."""
        internal_calls = [tc for tc in self.bundle.tool_calls if tc.service_id == "internal"]
        self.assertGreater(len(internal_calls), 0, "Expected some internal calls in trace")
        for tc in internal_calls:
            self.assertEqual(tc.channel, "local")

    def test_mcp_channel_on_tool_calls(self):
        mcp_calls = [tc for tc in self.bundle.tool_calls if tc.channel == "real_mcp"]
        self.assertGreater(len(mcp_calls), 0)


class TestEvidenceCard(unittest.TestCase):
    """§7: Provenance annotations"""

    @classmethod
    def setUpClass(cls):
        trace = _load_real_trace()
        if trace is None:
            raise unittest.SkipTest("Real trace file not available")
        cls.bundle = _make_bundle(trace)
        cls.card = build_evidence_card(cls.bundle)

    def test_card_has_provenance(self):
        self.assertIsNotNone(self.card.provenance)
        self.assertIn("total_evidence_items", self.card.provenance)
        self.assertIn("source_distribution", self.card.provenance)
        self.assertIn("original_confidence_pct", self.card.provenance)

    def test_card_has_fingerprint(self):
        self.assertTrue(self.card.evidence_fingerprint)
        self.assertEqual(len(self.card.evidence_fingerprint), 64)  # SHA-256

    def test_markdown_contains_provenance(self):
        md = render_evidence_card_markdown(self.card)
        self.assertIn("## \U0001f4cb Provenance", md)
        self.assertIn("Original Metadata %", md)

    def test_card_summary_has_expected_keys(self):
        expected = {"total_tool_calls", "services_discovered", "phases_recorded"}
        self.assertTrue(expected.issubset(set(self.card.summary.keys())))


class TestEvidenceChecker(unittest.TestCase):
    """§8: 19 quality checks (12 spec items → 16 base + 3 debrief checks)"""

    @classmethod
    def setUpClass(cls):
        trace = _load_real_trace()
        if trace is None:
            raise unittest.SkipTest("Real trace file not available")
        bundle = _make_bundle(trace)
        card = build_evidence_card(bundle)
        checker = EvidenceChecker(bundle, card)
        cls.report = checker.run_all()

    def test_runs_21_checks(self):
        self.assertEqual(len(self.report.checks), 21)

    def test_no_failures_on_real_trace(self):
        self.assertEqual(self.report.summary["failed"], 0)

    def test_overall_status_not_fail(self):
        self.assertIn(self.report.overall_status, ("PASS", "WARN"))

    def test_checks_have_research_category(self):
        for c in self.report.checks:
            self.assertIn(c.category, ("data", "logic"))
        data_names = {c.check_name for c in self.report.checks if c.category == "data"}
        self.assertIn("channel_classification", data_names)
        self.assertIn("service_coverage", {c.check_name for c in self.report.checks if c.category == "logic"})

    def test_summarize_evidence_dimensions(self):
        from evidence_checker import summarize_evidence_dimensions

        dims = summarize_evidence_dimensions(self.report.checks)
        self.assertEqual(set(dims.keys()), {"data", "logic"})
        for key in ("data", "logic"):
            self.assertIn(dims[key]["status"], ("PASS", "WARN", "FAIL"))
            self.assertGreater(dims[key]["total"], 0)


class TestRobustness(unittest.TestCase):
    """Graceful handling of malformed traces"""

    MALFORMED_TRACES = [
        ({}, "empty dict"),
        ({"session_id": "t", "events": []}, "empty events"),
        ({"session_id": "t", "events": None}, "None events"),
        ({"session_id": "x", "events": [None, "bad", 123, {}]}, "garbage events"),
        ({"session_id": "y", "events": [{"type": "log", "data": None, "timestamp": 1}]}, "null data"),
    ]

    def test_no_crash_on_malformed(self):
        """Non-v1 traces are rejected at adapter init."""
        for trace, label in self.MALFORMED_TRACES:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    TraceEvidenceAdapter(trace)

    def test_empty_trace_rejected(self):
        with self.assertRaises(ValueError):
            TraceEvidenceAdapter({})


class TestServiceInference(unittest.TestCase):
    """Edge cases for _infer_service_id — robustness against non-standard naming"""

    def _make_adapter_with_services(self, service_ids: list[str]) -> TraceEvidenceAdapter:
        """Create adapter with synthetic service events for prefix matching."""
        events = []
        for sid in service_ids:
            events.append({
                "type": "service",
                "data": {"id": sid, "name": sid, "status": "connected", "channel": "mcp"},
                "timestamp": 1000.0,
            })
        return TraceEvidenceAdapter({
            "session_id": "test",
            "metadata": V1_META,
            "events": events,
        })

    def test_exact_prefix_match(self):
        adapter = self._make_adapter_with_services(["weather-api", "db-service"])
        self.assertEqual(adapter._infer_service_id("weather-api_get_forecast"), "weather-api")
        self.assertEqual(adapter._infer_service_id("db-service_query"), "db-service")

    def test_longest_prefix_wins(self):
        """If services share prefixes, longest match wins."""
        adapter = self._make_adapter_with_services(["mcp-demo", "mcp-demo-openfda"])
        self.assertEqual(adapter._infer_service_id("mcp-demo-openfda_search"), "mcp-demo-openfda")

    def test_terminate_is_internal(self):
        adapter = self._make_adapter_with_services(["some-service"])
        self.assertEqual(adapter._infer_service_id("terminate"), "internal")

    def test_unknown_tool_is_unresolved(self):
        """Tools that don't match any service or known internal → 'unresolved'"""
        adapter = self._make_adapter_with_services(["alpha-svc"])
        self.assertEqual(adapter._infer_service_id("totally_random_tool"), "unresolved")

    def test_unknown_tool_without_service_events(self):
        """Without service discovery events, tool names are not inferred."""
        adapter = TraceEvidenceAdapter({"session_id": "t", "metadata": V1_META, "events": []})
        self.assertEqual(adapter._infer_service_id("mcp-demo-openfda_search"), "unresolved")

    def test_empty_tool_name(self):
        adapter = self._make_adapter_with_services(["svc"])
        result = adapter._infer_service_id("")
        self.assertIn(result, ("internal", "unresolved"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCLIIntegration(unittest.TestCase):
    """End-to-end CLI test: run run_pipeline.py as a subprocess."""

    @classmethod
    def setUpClass(cls):
        if not REAL_TRACE.exists():
            raise unittest.SkipTest("Real trace file not available")
        import subprocess
        import tempfile

        cls.tmpdir = tempfile.mkdtemp(prefix="te_cli_test_")
        cls.python = str(Path(__file__).parent.parent.parent / ".venv/bin/python")
        cls.script = str(Path(__file__).parent.parent / "run_pipeline.py")

        result = subprocess.run(
            [cls.python, cls.script, str(REAL_TRACE), "--output-dir", cls.tmpdir],
            capture_output=True, text=True, timeout=30
        )
        cls.returncode = result.returncode
        cls.stdout = result.stdout
        cls.stderr = result.stderr

    @classmethod
    def tearDownClass(cls):
        import shutil
        if hasattr(cls, 'tmpdir') and Path(cls.tmpdir).exists():
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_exit_code_zero(self):
        """Pipeline should exit 0 (not FAIL)."""
        if self.returncode != 0:
            self.fail(f"Exit code {self.returncode}\nstderr: {self.stderr[:500]}")

    def test_stdout_contains_pipeline_complete(self):
        self.assertIn("PIPELINE COMPLETE", self.stdout)

    def test_evidence_card_json_produced(self):
        """At least one ev-*.json (not *_checker_report.json, not *_config_draft.json)."""
        cards = [f for f in Path(self.tmpdir).iterdir()
                 if f.name.startswith("ev-") and f.name.endswith(".json")
                 and "_checker_report" not in f.name and "_config_draft" not in f.name]
        self.assertEqual(len(cards), 1, f"Expected 1 evidence card JSON, got: {[f.name for f in cards]}")
        # Validate JSON
        data = json.loads(cards[0].read_text())
        self.assertIn("evidence_id", data)
        self.assertIn("evidence_fingerprint", data)

    def test_evidence_card_md_produced(self):
        cards_md = [f for f in Path(self.tmpdir).iterdir()
                    if f.name.startswith("ev-") and f.name.endswith(".md")
                    and "_checker_report" not in f.name]
        self.assertEqual(len(cards_md), 1)
        content = cards_md[0].read_text()
        self.assertIn("# Evidence Card:", content)

    def test_checker_report_json_produced(self):
        reports = [f for f in Path(self.tmpdir).iterdir()
                   if "_checker_report.json" in f.name]
        self.assertEqual(len(reports), 1)
        data = json.loads(reports[0].read_text())
        self.assertIn("overall_status", data)
        self.assertIn("checks", data)
        self.assertEqual(len(data["checks"]), 21)

    def test_checker_report_md_produced(self):
        reports = [f for f in Path(self.tmpdir).iterdir()
                   if "_checker_report.md" in f.name]
        self.assertEqual(len(reports), 1)
        content = reports[0].read_text()
        self.assertIn("Evidence Checker Report", content)

    def test_config_draft_json_produced(self):
        drafts = [f for f in Path(self.tmpdir).iterdir()
                  if "_config_draft.json" in f.name]
        self.assertEqual(len(drafts), 1)
        data = json.loads(drafts[0].read_text())
        self.assertIn("evidence_id", data)
        self.assertIn("dispatch_sequence", data)

    def test_bundle_json_produced(self):
        bundles = [f for f in Path(self.tmpdir).iterdir()
                   if "_bundle.json" in f.name]
        self.assertEqual(len(bundles), 1)
        data = json.loads(bundles[0].read_text())
        self.assertIn("session_id", data)
        self.assertIn("tool_calls", data)

    def test_no_stderr_errors(self):
        """No tracebacks or ERROR messages in stderr."""
        self.assertNotIn("Traceback", self.stderr)
        self.assertNotIn("ERROR", self.stderr.upper().replace("error", "ERROR"))

    def test_output_file_count(self):
        """Pipeline should produce exactly 6 files."""
        files = list(Path(self.tmpdir).iterdir())
        self.assertEqual(len(files), 6,
                         f"Expected 6 output files, got {len(files)}: {[f.name for f in files]}")


class TestMarkdownInjection(unittest.TestCase):
    """Pass #15: Security — verify markdown injection from untrusted trace data is neutralized."""

    INJECTION_PAYLOADS = [
        "## Injected Header",
        "[evil](javascript:alert(1))",
        "![img](https://evil.com/track.png)",
        "<script>alert('xss')</script>",
        "```\ncode block breakout\n```",
        "| col1 | col2 |\n|---|---|\n| table | injection |",
        "`backtick` **bold** *italic* ~~strike~~",
        "line1\n# Header Injection\nline3",
    ]

    def _make_poisoned_trace(self, payload: str) -> dict:
        """Create a trace where tool output and planner reasoning contain injection payloads."""
        return {
            "session_id": "injection-test",
            "metadata": V1_META,
            "events": [
                {
                    "type": "service",
                    "data": {"id": payload, "name": payload, "status": "connected"},
                    "timestamp": 1000.0,
                },
                {
                    "type": "tool_call",
                    "data": {
                        "tool_name": payload,
                        "service_id": payload,
                        "input": {"query": payload},
                        "output": {"result": payload},
                        "latency_ms": 100,
                        "status": "success",
                    },
                    "timestamp": 1001.0,
                },
                {
                    "type": "phase",
                    "data": {"name": payload, "status": "completed"},
                    "timestamp": 1002.0,
                },
                {
                    "type": "iteration",
                    "data": {"iteration": 1, "action": payload, "reasoning": payload},
                    "timestamp": 1003.0,
                },
                {
                    "type": "planner",
                    "data": {"event": "plan_created", "content": payload},
                    "timestamp": 1004.0,
                },
                {
                    "type": "verification",
                    "data": {"success": True, "reason": payload, "checks_passed": 1, "checks_total": 1},
                    "timestamp": 1005.0,
                },
            ],
        }

    def test_no_raw_html_in_card_markdown(self):
        """HTML tags from trace data must not appear raw in rendered markdown."""
        trace = self._make_poisoned_trace("<script>alert('xss')</script>")
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        card = build_evidence_card(bundle)
        md = render_evidence_card_markdown(card)
        self.assertNotIn("<script>", md)
        self.assertNotIn("</script>", md)

    def test_no_header_injection_in_card(self):
        """Injected markdown headers from trace data must be neutralized."""
        trace = self._make_poisoned_trace("## Injected Header")
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        card = build_evidence_card(bundle)
        md = render_evidence_card_markdown(card)
        lines = md.split('\n')
        for line in lines:
            if "Injected Header" in line:
                self.assertFalse(
                    line.strip().startswith("## Injected"),
                    f"Header injection found: {line!r}"
                )

    def test_no_link_injection(self):
        """JavaScript links from trace data must not render as clickable markdown links."""
        trace = self._make_poisoned_trace("[evil](javascript:alert(1))")
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        card = build_evidence_card(bundle)
        md = render_evidence_card_markdown(card)
        self.assertNotIn("](javascript:", md)

    def test_all_payloads_produce_valid_card(self):
        """All injection payloads should produce a valid card without crashing."""
        for payload in self.INJECTION_PAYLOADS:
            with self.subTest(payload=payload[:30]):
                trace = self._make_poisoned_trace(payload)
                adapter = TraceEvidenceAdapter(trace)
                bundle = adapter.extract()
                card = build_evidence_card(bundle)
                md = render_evidence_card_markdown(card)
                self.assertIn("# Evidence Card", md)
                self.assertTrue(card.evidence_id)


class TestSchemaValidation(unittest.TestCase):
    """Pass #16: Validate pipeline outputs conform to JSON Schema definitions."""

    def _run_pipeline(self):
        """Run full pipeline and return (card_dict, report_dict)."""
        from dataclasses import asdict
        trace = _load_real_trace()
        if trace is None:
            self.skipTest("Real trace not available")
        adapter = TraceEvidenceAdapter(trace)
        bundle = adapter.extract()
        card = build_evidence_card(bundle)
        card_dict = card.to_dict()
        checker = EvidenceChecker(bundle, card)
        report = checker.run_all()
        report_dict = report.to_dict()
        return card_dict, report_dict

    def test_evidence_card_schema_valid(self):
        """Evidence card JSON must conform to evidence_card_schema.json."""
        from schema_validator import validate_evidence_card
        card_dict, _ = self._run_pipeline()
        result = validate_evidence_card(card_dict)
        self.assertTrue(result.valid, f"Card schema errors: {result.summary()}")

    def test_checker_report_schema_valid(self):
        """Checker report JSON must conform to checker_report_schema.json."""
        from schema_validator import validate_checker_report
        _, report_dict = self._run_pipeline()
        result = validate_checker_report(report_dict)
        self.assertTrue(result.valid, f"Report schema errors: {result.summary()}")

    def test_schema_rejects_invalid_card(self):
        """Schema validator must reject a card missing required fields."""
        from schema_validator import validate_evidence_card
        invalid_card = {"evidence_id": "test", "session_id": "test"}
        result = validate_evidence_card(invalid_card)
        self.assertFalse(result.valid)
        self.assertGreater(len(result.errors), 0)

    def test_schema_rejects_invalid_report(self):
        """Schema validator must reject a report missing required fields."""
        from schema_validator import validate_checker_report
        invalid_report = {"evidence_id": "test"}
        result = validate_checker_report(invalid_report)
        self.assertFalse(result.valid)
        self.assertGreater(len(result.errors), 0)


class TestRunPipelineConvenience(unittest.TestCase):
    """Tests for the one-call run_pipeline() convenience function."""

    def test_run_pipeline_with_path(self):
        """run_pipeline() accepts a file path and returns PipelineResult."""
        if not REAL_TRACE.exists():
            self.skipTest("Real trace not available")
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from trace_evidence import run_pipeline, PipelineResult
        result = run_pipeline(str(REAL_TRACE))
        self.assertIsInstance(result, PipelineResult)
        self.assertIsNotNone(result.bundle)
        self.assertIsNotNone(result.card)
        self.assertIsNotNone(result.report)
        self.assertGreater(len(result.card_md), 100)
        self.assertGreater(len(result.report_md), 100)

    def test_run_pipeline_with_dict(self):
        """run_pipeline() accepts a dict and returns PipelineResult."""
        if not REAL_TRACE.exists():
            self.skipTest("Real trace not available")
        import json
        trace_data = json.loads(REAL_TRACE.read_text())
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from trace_evidence import run_pipeline, PipelineResult
        result = run_pipeline(trace_data)
        self.assertIsInstance(result, PipelineResult)
        self.assertIn(result.report.overall_status, ("PASS", "WARN"))

    def test_run_pipeline_checker_message_clarity(self):
        """Checker detail message should group by field, not list indices."""
        if not REAL_TRACE.exists():
            self.skipTest("Real trace not available")
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from trace_evidence import run_pipeline
        result = run_pipeline(str(REAL_TRACE))
        for check in result.report.checks:
            if check.check_name == "tool_call_details_consistency":
                # Should NOT contain old format "N missing required fields"
                self.assertNotIn("missing required fields", check.detail)
                # Detail should be a non-empty informative string
                self.assertGreater(len(check.detail), 0)
                break


class TestTimestampRobustness(unittest.TestCase):
    """Edge-case tests for _normalize_ts and timestamp handling in evidence_card."""

    def test_normalize_ts_float(self):
        from evidence_card import _normalize_ts
        self.assertAlmostEqual(_normalize_ts(1700000000.0), 1700000000.0)

    def test_normalize_ts_int(self):
        from evidence_card import _normalize_ts
        self.assertEqual(_normalize_ts(1700000000), 1700000000.0)

    def test_normalize_ts_iso_string(self):
        from evidence_card import _normalize_ts
        ts = _normalize_ts("2024-01-15T10:30:00+00:00")
        self.assertIsInstance(ts, float)
        self.assertGreater(ts, 1700000000)

    def test_normalize_ts_iso_z_suffix(self):
        from evidence_card import _normalize_ts
        ts = _normalize_ts("2024-01-15T10:30:00Z")
        self.assertIsInstance(ts, float)
        self.assertGreater(ts, 1700000000)

    def test_normalize_ts_invalid_string(self):
        from evidence_card import _normalize_ts
        self.assertEqual(_normalize_ts("not-a-date"), 0.0)

    def test_normalize_ts_none_like(self):
        from evidence_card import _normalize_ts
        self.assertEqual(_normalize_ts(None), 0.0)

    def test_card_with_iso_timestamps_no_crash(self):
        """Regression: build_evidence_card must not crash on ISO string timestamps."""
        from trace_adapter import ToolCallEvidence, ServiceEvidence, TraceEvidenceBundle

        tc = ToolCallEvidence(
            tool_name="test_tool", service_id="svc1", direction="call",
            timestamp="2024-06-01T12:00:00Z",
            trace_event_index=0, channel="real_mcp"
        )
        svc = ServiceEvidence(
            service_id="svc1", status="connected", latency_ms=50,
            channel="real_mcp", tools=["test_tool"],
            timestamp="2024-06-01T12:00:01Z"
        )
        bundle = TraceEvidenceBundle(
            session_id="test-iso-ts",
            app_name="test", domain="test", mode="headless",
            strategy={"type": "test"},
            tool_calls=[tc], services=[svc],
            completion=None, planner_thoughts=[], missing_evidence=[]
        )
        card = build_evidence_card(bundle)
        self.assertIn("start", card.timeline)
        self.assertIsInstance(card.timeline["duration_sec"], float)


class TestSanitizationSecurity(unittest.TestCase):
    """Test that malicious inputs are sanitized in card output."""

    def test_redact_secrets_api_key(self):
        from sanitize import redact_secrets
        text = "Using key sk-abc123def456ghi789jkl012mno345pqr678"
        result = redact_secrets(text)
        self.assertNotIn("sk-abc123", result)
        self.assertIn("[REDACTED", result)

    def test_redact_secrets_bearer_token(self):
        from sanitize import redact_secrets
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        result = redact_secrets(text)
        self.assertNotIn("eyJhbGci", result)
        self.assertIn("[REDACTED", result)

    def test_sanitize_md_cell_injection(self):
        from sanitize import sanitize_md_cell
        malicious = "cell | injected | extra"
        result = sanitize_md_cell(malicious)
        # Pipe chars are escaped with backslash — won't break markdown tables
        self.assertNotIn(" | ", result)  # raw unescaped pipe gone

    def test_sanitize_md_block_html_injection(self):
        from sanitize import sanitize_md_block
        malicious = '<script>alert("xss")</script>'
        result = sanitize_md_block(malicious)
        self.assertNotIn("<script>", result)

    def test_sanitize_identifier_length_limit(self):
        from sanitize import sanitize_identifier
        long_id = "a" * 200
        result = sanitize_identifier(long_id)
        self.assertLessEqual(len(result), 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
