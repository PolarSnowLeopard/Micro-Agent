"""End-to-End Integration Tests for trace_evidence

Tests the full pipeline from a user's perspective:
1. CLI subprocess execution (exit code, output files, JSON validity)
2. Package import API (from trace_evidence import ...)
3. Error handling for invalid inputs
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Paths
PACKAGE_DIR = Path(__file__).parent.parent
PROJECT_ROOT = PACKAGE_DIR.parent
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
PIPELINE_SCRIPT = PACKAGE_DIR / "run_pipeline.py"
REAL_TRACE = Path(__file__).parent / "fixtures" / "minimal_v1_trace.json"


def _skip_if_no_trace():
    if not REAL_TRACE.exists():
        raise unittest.SkipTest(f"Real trace not found: {REAL_TRACE}")


def _skip_if_no_python():
    if not PYTHON.exists():
        raise unittest.SkipTest(f"Python not found: {PYTHON}")


class TestCLIEndToEnd(unittest.TestCase):
    """Test the pipeline CLI as a subprocess — the way a real user runs it."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_trace()
        _skip_if_no_python()
        cls.tmpdir = tempfile.mkdtemp(prefix="te_e2e_")
        # Run the pipeline once for all tests
        result = subprocess.run(
            [str(PYTHON), str(PIPELINE_SCRIPT), str(REAL_TRACE), "-o", cls.tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
        )
        cls.result = result

    @classmethod
    def tearDownClass(cls):
        # Cleanup temp dir
        import shutil
        if hasattr(cls, 'tmpdir') and os.path.exists(cls.tmpdir):
            shutil.rmtree(cls.tmpdir)

    def test_exit_code_zero(self):
        """Pipeline exits successfully."""
        self.assertEqual(
            self.result.returncode, 0,
            f"Pipeline failed with stderr:\n{self.result.stderr}"
        )

    def test_produces_evidence_card_json(self):
        """Pipeline produces an evidence card JSON file (ev-*.json)."""
        json_files = [f for f in Path(self.tmpdir).glob("ev-*.json")
                      if "checker" not in f.name and "config" not in f.name
                      and "bundle" not in f.name]
        self.assertGreaterEqual(len(json_files), 1, "No evidence card JSON produced")

    def test_produces_evidence_card_markdown(self):
        """Pipeline produces an evidence card markdown file (ev-*.md)."""
        md_files = list(Path(self.tmpdir).glob("ev-*.md"))
        self.assertGreaterEqual(len(md_files), 1, "No evidence card markdown produced")

    def test_produces_checker_report_json(self):
        """Pipeline produces a checker_report.json file."""
        json_files = list(Path(self.tmpdir).glob("*_checker_report.json"))
        self.assertGreaterEqual(len(json_files), 1, "No checker_report.json produced")

    def test_produces_config_attachment(self):
        """Pipeline produces a config_draft.json file."""
        json_files = list(Path(self.tmpdir).glob("*_config_draft.json"))
        self.assertGreaterEqual(len(json_files), 1, "No config_draft.json produced")

    def test_evidence_card_json_valid(self):
        """Evidence card JSON is valid with required top-level fields."""
        json_files = [f for f in Path(self.tmpdir).glob("ev-*.json")
                      if "checker" not in f.name and "config" not in f.name
                      and "bundle" not in f.name]
        if not json_files:
            self.skipTest("No evidence card JSON")
        data = json.loads(json_files[0].read_text())
        self.assertIn("evidence_id", data)
        self.assertIn("session_id", data)
        self.assertIn("generated_at", data)

    def test_checker_report_json_valid(self):
        """checker_report.json is valid JSON with expected structure."""
        json_files = list(Path(self.tmpdir).glob("*_checker_report.json"))
        if not json_files:
            self.skipTest("No checker_report.json")
        data = json.loads(json_files[0].read_text())
        self.assertIn("checks", data)
        self.assertIsInstance(data["checks"], list)
        self.assertGreater(len(data["checks"]), 0)

    def test_no_stderr_errors(self):
        """Pipeline stderr should not contain Python tracebacks."""
        self.assertNotIn("Traceback", self.result.stderr)
        self.assertNotIn("Error", self.result.stderr)


class TestCLIErrorHandling(unittest.TestCase):
    """Test CLI graceful error handling with precise exit codes.

    Exit code convention:
      1 = file-level error (missing file, permission denied)
      2 = data-level error (invalid JSON, wrong type, empty)
    """

    def setUp(self):
        _skip_if_no_python()

    def _run_pipeline(self, args):
        return subprocess.run(
            [str(PYTHON), str(PIPELINE_SCRIPT)] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_missing_trace_file_exit_code(self):
        """Missing file → exit 1, no traceback, user-friendly message."""
        result = self._run_pipeline(["/nonexistent/trace.json"])
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        combined = (result.stdout + result.stderr).lower()
        self.assertIn("not found", combined)

    def test_invalid_json_trace_exit_code(self):
        """Invalid JSON → exit 2, no traceback, user-friendly message."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            tmpfile = f.name
        try:
            result = self._run_pipeline([tmpfile])
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
        finally:
            os.unlink(tmpfile)

    def test_empty_file_trace_exit_code(self):
        """Empty file → exit 2, no traceback."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("")
            tmpfile = f.name
        try:
            result = self._run_pipeline([tmpfile])
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
        finally:
            os.unlink(tmpfile)

    def test_json_array_trace_exit_code(self):
        """JSON array (not object) → exit 2, no traceback."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('[{"key": "value"}]')
            tmpfile = f.name
        try:
            result = self._run_pipeline([tmpfile])
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
        finally:
            os.unlink(tmpfile)

    def test_help_flag(self):
        """--help exits 0 and shows usage."""
        result = self._run_pipeline(["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage", result.stdout.lower())


class TestPackageImportAPI(unittest.TestCase):
    """Test that trace_evidence is importable as a package with clean API."""

    def test_import_package(self):
        """Package imports without error."""
        result = subprocess.run(
            [str(PYTHON), "-c", "import trace_evidence"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(
            result.returncode, 0,
            f"Package import failed:\n{result.stderr}"
        )

    def test_all_exports_accessible(self):
        """All __all__ symbols are importable."""
        code = """
import trace_evidence
for name in trace_evidence.__all__:
    obj = getattr(trace_evidence, name)
    assert obj is not None, f"{name} is None"
print(f"OK: {len(trace_evidence.__all__)} symbols")
"""
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(
            result.returncode, 0,
            f"Symbol check failed:\n{result.stderr}"
        )

    def test_programmatic_pipeline(self):
        """Run pipeline programmatically via package API."""
        _skip_if_no_trace()
        code = f"""
import json, sys
sys.path.insert(0, "{PACKAGE_DIR}")
from trace_adapter import TraceEvidenceAdapter
from evidence_card import build_evidence_card
from evidence_checker import EvidenceChecker

trace_data = json.loads(open("{REAL_TRACE}").read())
adapter = TraceEvidenceAdapter(trace_data)
bundle = adapter.extract()
card = build_evidence_card(bundle)
assert card.evidence_id, "No evidence_id"
checker = EvidenceChecker(bundle, card)
report = checker.run_all()
assert len(report.checks) > 0, "No checks"
print(f"OK: {{len(report.checks)}} checks, status={{report.overall_status}}")
"""
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            result.returncode, 0,
            f"Programmatic pipeline failed:\n{result.stderr}"
        )


class TestCrossArtifactConsistency(unittest.TestCase):
    """Validate cross-references between output artifacts.

    A reviewer's first check: do the card, checker report, and config draft
    all agree on identity fields? These tests catch drift between generators.
    """

    @classmethod
    def setUpClass(cls):
        _skip_if_no_trace()
        _skip_if_no_python()
        cls.tmpdir = tempfile.mkdtemp(prefix="te_xref_")
        result = subprocess.run(
            [str(PYTHON), str(PIPELINE_SCRIPT), str(REAL_TRACE), "-o", cls.tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"Pipeline failed: {result.stderr[:200]}")

        outdir = Path(cls.tmpdir)
        # Load all JSON artifacts
        card_files = [f for f in outdir.glob("ev-*.json")
                      if "checker" not in f.name and "config" not in f.name
                      and "bundle" not in f.name]
        checker_files = list(outdir.glob("*_checker_report.json"))
        config_files = list(outdir.glob("*_config_draft.json"))

        cls.card = json.loads(card_files[0].read_text()) if card_files else None
        cls.checker = json.loads(checker_files[0].read_text()) if checker_files else None
        cls.config = json.loads(config_files[0].read_text()) if config_files else None

        # Load markdown artifacts
        card_md_files = [f for f in outdir.glob("ev-*.md")
                         if "checker" not in f.name]
        checker_md_files = list(outdir.glob("*_checker_report.md"))
        cls.card_md = card_md_files[0].read_text() if card_md_files else ""
        cls.checker_md = checker_md_files[0].read_text() if checker_md_files else ""

    @classmethod
    def tearDownClass(cls):
        import shutil
        if hasattr(cls, 'tmpdir') and os.path.exists(cls.tmpdir):
            shutil.rmtree(cls.tmpdir)

    def test_evidence_id_consistent_across_artifacts(self):
        """evidence_id must be identical in card, checker, and config."""
        self.assertIsNotNone(self.card, "No evidence card JSON")
        self.assertIsNotNone(self.checker, "No checker report JSON")
        self.assertIsNotNone(self.config, "No config draft JSON")

        eid = self.card["evidence_id"]
        self.assertTrue(eid.startswith("ev-"), f"Bad evidence_id format: {eid}")
        self.assertEqual(self.checker.get("evidence_id"), eid,
                         "Checker report evidence_id doesn't match card")
        self.assertEqual(self.config.get("evidence_id"), eid,
                         "Config draft evidence_id doesn't match card")

    def test_session_id_consistent_across_artifacts(self):
        """session_id must be identical in card, checker, and config."""
        self.assertIsNotNone(self.card)
        sid = self.card["session_id"]
        self.assertTrue(sid.startswith("sim-"), f"Bad session_id format: {sid}")
        self.assertEqual(self.checker.get("session_id"), sid,
                         "Checker report session_id doesn't match card")
        self.assertEqual(self.config.get("session_id"), sid,
                         "Config draft session_id doesn't match card")

    def test_markdown_references_correct_evidence_id(self):
        """Markdown reports must mention the same evidence_id as JSON."""
        self.assertIsNotNone(self.card)
        eid = self.card["evidence_id"]
        self.assertIn(eid, self.card_md,
                      "Evidence card markdown doesn't contain its own evidence_id")
        self.assertIn(eid, self.checker_md,
                      "Checker report markdown doesn't contain the evidence_id")

    def test_checker_check_count_matches_json(self):
        """Checker report JSON checks count must match what's in the MD table."""
        self.assertIsNotNone(self.checker)
        json_count = len(self.checker["checks"])
        # MD table has rows like "| 1 | check_name | ..."
        import re
        table_rows = re.findall(r'^\| \d+ \|', self.checker_md, re.MULTILINE)
        self.assertEqual(
            len(table_rows), json_count,
            f"MD table has {len(table_rows)} rows but JSON has {json_count} checks"
        )

    def test_config_draft_services_nonempty(self):
        """Config draft must list at least one tool channel."""
        self.assertIsNotNone(self.config)
        channels = self.config.get("tool_channels", [])
        self.assertGreater(len(channels), 0, "Config draft has no tool_channels")
        # Each channel should have required fields
        for ch in channels:
            self.assertIn("serviceId", ch, f"Channel missing serviceId: {ch}")
            self.assertIn("channel", ch, f"Channel missing channel type: {ch}")

    def test_fingerprint_present_and_format(self):
        """Evidence card must have an evidence_fingerprint in hex format."""
        self.assertIsNotNone(self.card)
        fp = self.card.get("evidence_fingerprint", "")
        self.assertTrue(len(fp) >= 8, f"Fingerprint too short: {fp}")
        # Should be hex characters (sha256)
        import re
        self.assertRegex(fp, r'^[0-9a-f]+$',
                         f"Fingerprint not hex format: {fp}")


if __name__ == "__main__":
    unittest.main()
