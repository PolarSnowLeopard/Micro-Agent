"""Tests that pipeline output conforms to the evidence card JSON Schema.

These tests validate that:
1. The schema itself is well-formed (meta-validation)
2. Real pipeline output passes schema validation
3. Known-bad documents are correctly rejected
"""
import json
import subprocess
import unittest
from pathlib import Path

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "evidence_card_schema.json"
PIPELINE_SCRIPT = PROJECT_ROOT / "run_pipeline.py"
PYTHON = PROJECT_ROOT.parent / ".venv" / "bin" / "python"
TRACE_FILE = PROJECT_ROOT.parent / "workspace" / "data" / "traces" / "sim-b963f6d83a89.json"


def _skip_if_no_jsonschema():
    if not HAS_JSONSCHEMA:
        raise unittest.SkipTest("jsonschema not installed")


def _skip_if_no_python():
    if not PYTHON.exists():
        raise unittest.SkipTest(f"Python not found at {PYTHON}")


def _load_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestSchemaMetaValidation(unittest.TestCase):
    """Verify the schema itself is valid JSON Schema."""

    def test_schema_is_valid_json(self):
        """Schema file is parseable JSON."""
        schema = _load_schema()
        self.assertIsInstance(schema, dict)
        self.assertIn("$schema", schema)

    def test_schema_is_valid_draft2020(self):
        """Schema conforms to JSON Schema Draft 2020-12."""
        schema = _load_schema()
        # Validate schema against its own meta-schema
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)

    def test_schema_has_required_fields(self):
        """Schema declares required top-level fields."""
        schema = _load_schema()
        required = schema.get("required", [])
        # These are the absolute minimum for a valid evidence card
        for field in ["evidence_id", "session_id", "generated_at", "summary", "timeline"]:
            self.assertIn(field, required, f"'{field}' should be required")


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestPipelineOutputValidation(unittest.TestCase):
    """Validate real pipeline output against the schema."""

    def setUp(self):
        _skip_if_no_python()
        if not TRACE_FILE.exists():
            self.skipTest(f"Trace file not found: {TRACE_FILE}")

    def _run_pipeline_and_get_evidence_card(self):
        """Run pipeline and return the evidence card JSON."""
        import tempfile
        import os
        outdir = tempfile.mkdtemp(prefix="schema_test_")
        result = subprocess.run(
            [str(PYTHON), str(PIPELINE_SCRIPT), str(TRACE_FILE), "--output-dir", outdir],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, f"Pipeline failed: {result.stderr}")
        # Find evidence card file (starts with 'ev-', exclude auxiliary files)
        ev_files = [
            f for f in Path(outdir).glob("ev-*.json")
            if "_config_draft" not in f.name and "_checker_report" not in f.name
        ]
        self.assertTrue(ev_files, f"No evidence card found in {outdir}")
        card = json.loads(ev_files[0].read_text(encoding="utf-8"))
        # Cleanup
        import shutil
        shutil.rmtree(outdir, ignore_errors=True)
        return card

    def test_evidence_card_validates(self):
        """Pipeline evidence card passes full schema validation."""
        schema = _load_schema()
        card = self._run_pipeline_and_get_evidence_card()
        validator = jsonschema.Draft202012Validator(schema)
        errors = list(validator.iter_errors(card))
        if errors:
            msg_lines = [f"Schema validation failed with {len(errors)} error(s):"]
            for e in errors[:5]:
                path = ".".join(str(p) for p in e.absolute_path)
                msg_lines.append(f"  [{path}] {e.message}")
            self.fail("\n".join(msg_lines))

    def test_evidence_card_has_nonempty_summary(self):
        """Evidence card summary contains meaningful data."""
        card = self._run_pipeline_and_get_evidence_card()
        summary = card.get("summary", {})
        self.assertGreater(summary.get("total_tool_calls", 0), 0)
        self.assertGreater(summary.get("unique_services_called", 0), 0)

    def test_evidence_card_timeline_consistent(self):
        """Timeline start < end and duration > 0."""
        card = self._run_pipeline_and_get_evidence_card()
        timeline = card.get("timeline", {})
        self.assertLess(timeline["start"], timeline["end"])
        self.assertGreater(timeline["duration_sec"], 0)


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestSchemaRejectsInvalid(unittest.TestCase):
    """Verify schema correctly rejects malformed evidence cards."""

    def setUp(self):
        self.schema = _load_schema()
        self.validator = jsonschema.Draft202012Validator(self.schema)

    def test_rejects_empty_object(self):
        """Empty object fails validation (missing required fields)."""
        self.assertFalse(self.validator.is_valid({}))

    def test_rejects_missing_evidence_id(self):
        """Object without evidence_id is invalid."""
        doc = {"session_id": "x", "generated_at": "2025-01-01T00:00:00Z"}
        self.assertFalse(self.validator.is_valid(doc))

    def test_rejects_wrong_type_summary(self):
        """summary must be an object, not a string."""
        doc = {
            "evidence_id": "ev-test",
            "session_id": "s1",
            "generated_at": "2025-01-01T00:00:00Z",
            "summary": "not an object",
        }
        self.assertFalse(self.validator.is_valid(doc))

    def test_rejects_negative_tool_calls(self):
        """Negative tool call count is invalid."""
        doc = {
            "evidence_id": "ev-test",
            "session_id": "s1",
            "generated_at": "2025-01-01T00:00:00Z",
            "summary": {"total_tool_calls": -1},
        }
        self.assertFalse(self.validator.is_valid(doc))

    def test_rejects_extra_toplevel_properties(self):
        """additionalProperties: false rejects unknown keys."""
        doc = {
            "evidence_id": "ev-test",
            "session_id": "s1",
            "generated_at": "2025-01-01T00:00:00Z",
            "app_name": "test",
            "domain": "test",
            "mode": "test",
            "strategy": {},
            "summary": {"total_tool_calls": 1},
            "timeline": {"start": 1.0, "end": 2.0, "duration_sec": 1.0},
            "tool_call_summary": {},
            "services_discovered": [],
            "verification": {},
            "completion": {},
            "missing_evidence": [],
            "tool_call_details": [],
            "planner_events": [],
            "provenance": {},
            "evidence_fingerprint": "a" * 64,
            "UNKNOWN_FIELD": "should_fail",
        }
        self.assertFalse(self.validator.is_valid(doc))


# ─────────────────────────────────────────────────────────────────────
# Checker Report Schema Validation Tests
# ─────────────────────────────────────────────────────────────────────
CHECKER_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "checker_report_schema.json"


def _load_checker_schema():
    return json.loads(CHECKER_SCHEMA_PATH.read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestCheckerReportSchemaMeta(unittest.TestCase):
    """Verify checker_report schema itself is valid."""

    def test_checker_schema_is_valid_json(self):
        schema = _load_checker_schema()
        self.assertIsInstance(schema, dict)
        self.assertIn("$schema", schema)

    def test_checker_schema_is_valid_draft2020(self):
        schema = _load_checker_schema()
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)

    def test_checker_schema_requires_schema_version(self):
        schema = _load_checker_schema()
        self.assertIn("schema_version", schema.get("required", []))


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestCheckerReportOutputValidation(unittest.TestCase):
    """Validate real pipeline checker_report.json against schema."""

    def setUp(self):
        _skip_if_no_python()
        if not TRACE_FILE.exists():
            self.skipTest(f"Trace file not found: {TRACE_FILE}")

    def _run_pipeline_and_get_checker_report(self):
        import tempfile, shutil
        outdir = tempfile.mkdtemp(prefix="checker_schema_test_")
        result = subprocess.run(
            [str(PYTHON), str(PIPELINE_SCRIPT), str(TRACE_FILE), "--output-dir", outdir],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, f"Pipeline failed: {result.stderr}")
        cr_files = list(Path(outdir).glob("*checker_report.json"))
        self.assertTrue(cr_files, f"No checker_report.json in {outdir}")
        report = json.loads(cr_files[0].read_text(encoding="utf-8"))
        shutil.rmtree(outdir, ignore_errors=True)
        return report

    def test_checker_report_validates(self):
        """Pipeline checker_report passes full schema validation."""
        schema = _load_checker_schema()
        report = self._run_pipeline_and_get_checker_report()
        validator = jsonschema.Draft202012Validator(schema)
        errors = list(validator.iter_errors(report))
        if errors:
            msgs = [f"  [{'.'.join(str(p) for p in e.absolute_path)}] {e.message}" for e in errors[:5]]
            self.fail(f"Schema validation failed:\n" + "\n".join(msgs))

    def test_checker_report_has_schema_version(self):
        """Checker report includes schema_version field."""
        report = self._run_pipeline_and_get_checker_report()
        self.assertEqual(report.get("schema_version"), "1.0.0")

    def test_checker_report_has_checks(self):
        """Checker report has non-empty checks array."""
        report = self._run_pipeline_and_get_checker_report()
        self.assertGreater(len(report.get("checks", [])), 0)

    def test_checker_report_overall_status_valid(self):
        """overall_status is one of PASS/WARN/FAIL."""
        report = self._run_pipeline_and_get_checker_report()
        self.assertIn(report["overall_status"], ["PASS", "WARN", "FAIL"])


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestCheckerReportSchemaRejects(unittest.TestCase):
    """Verify checker_report schema correctly rejects invalid docs."""

    def setUp(self):
        self.schema = _load_checker_schema()
        self.validator = jsonschema.Draft202012Validator(self.schema)

    def test_rejects_empty_object(self):
        self.assertFalse(self.validator.is_valid({}))

    def test_rejects_missing_schema_version(self):
        doc = {
            "evidence_id": "ev-x", "session_id": "s1",
            "checked_at": "2025-01-01T00:00:00Z",
            "overall_status": "PASS", "summary": {},
            "checks": [],
        }
        self.assertFalse(self.validator.is_valid(doc))

    def test_rejects_invalid_overall_status(self):
        doc = {
            "schema_version": "1.0.0",
            "evidence_id": "ev-x", "session_id": "s1",
            "checked_at": "2025-01-01T00:00:00Z",
            "overall_status": "UNKNOWN", "summary": {},
            "checks": [],
        }
        self.assertFalse(self.validator.is_valid(doc))

    def test_rejects_check_missing_name(self):
        doc = {
            "schema_version": "1.0.0",
            "evidence_id": "ev-x", "session_id": "s1",
            "checked_at": "2025-01-01T00:00:00Z",
            "overall_status": "PASS", "summary": {},
            "checks": [{"status": "PASS", "message": "ok"}],
        }
        self.assertFalse(self.validator.is_valid(doc))


if __name__ == "__main__":
    unittest.main()
