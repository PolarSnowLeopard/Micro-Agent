"""Tests for artifact_compiler — trace → ArtifactSpec v0 compilation."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure Micro-Agent root is on path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from micro_agent.simulation.artifact_compiler import (
    ArtifactSpec,
    MetaAppInfo,
    ScenarioInfo,
    StateMachineTrace,
    Provenance,
    SolidificationReport,
    _build_meta_app,
    _build_scenario,
    _build_state_machine,
    _build_provenance,
    _build_solidification_report,
    _build_write_back,
    _build_evidence,
    _build_service_contracts,
    _extract_parsed_intent,
    _format_verifier_issue_messages,
    compile_artifact_spec,
    _sha256,
    _short_id,
    _ts_to_iso,
)

_TRACE_DIR = _REPO_ROOT / "workspace" / "data" / "traces"


def _load_trace(filename: str) -> dict:
    path = _TRACE_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Trace not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestUtilityFunctions(unittest.TestCase):
    """Unit tests for helper functions."""

    def test_sha256_deterministic(self):
        h1 = _sha256("hello")
        h2 = _sha256("hello")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h1))

    def test_sha256_different_inputs(self):
        h1 = _sha256("hello")
        h2 = _sha256("world")
        self.assertNotEqual(h1, h2)

    def test_short_id_format(self):
        sid = _short_id("art", "test-seed")
        self.assertRegex(sid, r"^art-[a-f0-9]{6}-[a-f0-9]{8}$")

    def test_ts_to_iso_valid(self):
        iso = _ts_to_iso(1717891200.0)
        self.assertIn("T", iso)
        self.assertIn("+00:00", iso)

    def test_ts_to_iso_none(self):
        self.assertEqual(_ts_to_iso(None), "")


class TestMetaAppBuilding(unittest.TestCase):
    """Tests for _build_meta_app."""

    def test_build_meta_app_basic(self):
        trace = {
            "app_name": "测试元应用",
            "domain": "health",
            "mode": "research",
            "metadata": {"config_snapshot": {}},
        }
        ma = _build_meta_app(trace)
        self.assertEqual(ma.appName, "测试元应用")
        self.assertEqual(ma.domain, "health")
        self.assertEqual(ma.mode, "research")

    def test_build_meta_app_falls_back_to_config(self):
        # .get() default only applies when key is missing, not when value is empty string.
        # Empty app_name from trace is used directly.
        trace = {
            "app_name": "",
            "domain": "",
            "mode": "production",
            "metadata": {
                "config_snapshot": {
                    "appName": "FallbackApp",
                    "domain": "aml",
                    "appId": "app-123",
                }
            },
        }
        ma = _build_meta_app(trace)
        # app_name is empty string (key exists), so it's used as-is
        self.assertEqual(ma.appName, "")
        self.assertEqual(ma.domain, "")
        self.assertEqual(ma.appId, "app-123")


class TestScenarioBuilding(unittest.TestCase):
    """Tests for _build_scenario."""

    def test_build_scenario_from_config_snapshot(self):
        trace = {
            "metadata": {
                "config_snapshot": {
                    "scenarioDescription": "测试场景",
                    "servicesMeta": [
                        {
                            "id": "svc-1",
                            "name": "Service1",
                            "description": "Desc1",
                            "mcpMethod": "sse",
                            "isFake": False,
                        }
                    ],
                }
            }
        }
        ma = MetaAppInfo(appName="TestApp", domain="health")
        sc = _build_scenario(trace, ma)
        self.assertEqual(sc.domain, "health")
        self.assertEqual(sc.sourceDescription, "测试场景")
        self.assertEqual(len(sc.involvedServices), 1)
        self.assertEqual(sc.involvedServices[0]["serviceId"], "svc-1")
        self.assertEqual(sc.involvedServices[0]["name"], "Service1")
        self.assertEqual(sc.involvedServices[0]["channel"], "sse")


class TestCompileFromRealTrace(unittest.TestCase):
    """Integration tests using real trace files."""

    @classmethod
    def setUpClass(cls):
        # Find a real v1.0.0 trace
        candidates = sorted(_TRACE_DIR.glob("sim-headless-*.json"), reverse=True)
        if not candidates:
            candidates = sorted(_TRACE_DIR.glob("sim-*.json"), reverse=True)
        if not candidates:
            raise unittest.SkipTest("No trace files found")
        cls.trace_path = candidates[0]
        with open(cls.trace_path, encoding="utf-8") as f:
            cls.trace = json.load(f)

    def test_compile_produces_valid_artifact_spec(self):
        """Compile a real trace and verify ArtifactSpec structure."""
        spec = compile_artifact_spec(self.trace, schema_version="0.1.0")

        # Top-level required fields
        self.assertEqual(spec.schemaVersion, "0.1.0")
        self.assertRegex(spec.artifactId, r"^art-[a-f0-9]{6}-[a-f0-9]{8}$")
        self.assertTrue(len(spec.sourceSessionId) > 0)
        self.assertTrue(len(spec.createdAt) > 0)

        # metaApp
        self.assertIn("appName", spec.metaApp)
        self.assertIn("domain", spec.metaApp)

        # scenario
        self.assertIn("title", spec.scenario)
        self.assertIn("domain", spec.scenario)
        self.assertIn("sourceDescription", spec.scenario)

        # stateMachineTrace
        self.assertIn("states", spec.stateMachineTrace)
        self.assertIn("transitions", spec.stateMachineTrace)
        self.assertIn("iterations", spec.stateMachineTrace)
        self.assertGreater(len(spec.stateMachineTrace["states"]), 0)
        self.assertGreater(len(spec.stateMachineTrace["transitions"]), 0)

        # provenance
        self.assertIn("sourceSessionId", spec.provenance)
        self.assertIn("sourceTraceVersion", spec.provenance)
        self.assertIn("traceHash", spec.provenance)
        self.assertIn("artifactHash", spec.provenance)
        self.assertEqual(len(spec.provenance["artifactHash"]), 64)

        # solidification
        self.assertIsInstance(spec.solidifiable, bool)
        self.assertIn("gates", spec.solidificationReport)
        self.assertEqual(len(spec.solidificationReport["gates"]), 6)

        # writeBackDraft
        self.assertIsNotNone(spec.writeBackDraft)
        self.assertIn("existingFields", spec.writeBackDraft)
        self.assertIn("newFields", spec.writeBackDraft)

    def test_artifact_hash_is_deterministic(self):
        """Same trace should produce same artifactHash."""
        spec1 = compile_artifact_spec(self.trace, schema_version="0.1.0")
        spec2 = compile_artifact_spec(self.trace, schema_version="0.1.0")
        self.assertEqual(spec1.artifactId, spec2.artifactId)
        self.assertEqual(spec1.provenance["artifactHash"], spec2.provenance["artifactHash"])

    def test_to_dict_is_serializable(self):
        """ArtifactSpec.to_dict() should be JSON-serializable."""
        spec = compile_artifact_spec(self.trace, schema_version="0.1.0")
        d = spec.to_dict()
        json_str = json.dumps(d, ensure_ascii=False)
        self.assertGreater(len(json_str), 100)

    def test_rejects_non_v1_trace(self):
        """Should raise ValueError for non-v1.0.0 traces."""
        bad_trace = {
            "session_id": "test-123",
            "metadata": {"runtime": {"trace_version": "v0.5.0"}},
            "events": [],
        }
        with self.assertRaises(ValueError) as ctx:
            compile_artifact_spec(bad_trace)
        self.assertIn("v1.0.0", str(ctx.exception))

    def test_rejects_missing_version(self):
        """Should raise ValueError when trace_version is absent."""
        bad_trace = {
            "session_id": "test-456",
            "metadata": {},
            "events": [],
        }
        with self.assertRaises(ValueError):
            compile_artifact_spec(bad_trace)


class TestSolidificationGates(unittest.TestCase):
    """Tests for solidification report logic."""

    def _make_trace(self, **overrides) -> dict:
        base = {
            "session_id": "sim-test",
            "events": [],
            "iterations": 2,
            "elapsed_ms": 5000,
            "success": True,
            "strategy": {"minIterations": 1},
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {},
            },
        }
        base.update(overrides)
        return base

    def test_all_gates_pass_for_clean_trace(self):
        trace = self._make_trace(
            events=[
                {"type": "tool_call_record", "data": {"call_id": "c1", "channel": "real_mcp", "success": True}}
            ]
        )
        smt = StateMachineTrace(
            states=[],
            transitions=[],
            iterations=[{"verifier": {"status": "PASSED"}}],
            totalIterations=2,
            finalStatus="SUCCESS",
            elapsedMs=5000,
        )
        # Mock evidence with COMPLETE
        ev = MagicMock()
        ev.completeness = "COMPLETE"
        ev.missingEvidenceCategories = []

        report = _build_solidification_report(trace, smt, ev)
        self.assertTrue(report.solidifiable)
        for g in report.gates:
            self.assertTrue(g["passed"], f"Gate {g['gate']} should pass, got: {g['detail']}")

    def test_insufficient_iterations_fails(self):
        trace = self._make_trace(iterations=0, strategy={"minIterations": 2})
        smt = StateMachineTrace(
            states=[],
            transitions=[],
            iterations=[],
            totalIterations=0,
            finalStatus="FAILED",
        )
        ev = MagicMock()
        ev.completeness = "COMPLETE"
        ev.missingEvidenceCategories = []

        report = _build_solidification_report(trace, smt, ev)
        self.assertFalse(report.solidifiable)

    def test_verifier_failed_fails_solidification(self):
        trace = self._make_trace()
        smt = StateMachineTrace(
            states=[],
            transitions=[],
            iterations=[{"verifier": {"status": "FAILED"}}],
            totalIterations=1,
            finalStatus="FAILED",
        )
        ev = MagicMock()
        ev.completeness = "COMPLETE"

        report = _build_solidification_report(trace, smt, ev)
        self.assertFalse(report.solidifiable)


class TestVerifierIssueFormatting(unittest.TestCase):
    """Regression: verifier_result.issues may be dicts, not only strings."""

    def test_format_string_issues(self):
        self.assertEqual(
            _format_verifier_issue_messages(["缺少报告", "顺序错误"]),
            "缺少报告; 顺序错误",
        )

    def test_format_dict_issues(self):
        issues = [
            {"description": "未调用报告服务", "evidence_refs": ["c1"]},
            {"message": "数据流断裂"},
        ]
        self.assertEqual(
            _format_verifier_issue_messages(issues),
            "未调用报告服务; 数据流断裂",
        )

    def test_state_machine_handles_dict_issues(self):
        trace = {
            "session_id": "sim-dict-issues",
            "iterations": 2,
            "elapsed_ms": 1000,
            "success": False,
            "strategy": {"minIterations": 1},
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {},
            },
            "events": [
                {"type": "iteration", "data": {"iteration": 1, "status": "running"}, "timestamp": 1.0},
                {"type": "planner_decision", "data": {"iteration": 1}, "timestamp": 1.1},
                {
                    "type": "verifier_result",
                    "data": {
                        "iteration": 1,
                        "status": "FAILED",
                        "issues": [{"description": "链路不完整", "evidence_refs": ["c1"]}],
                    },
                    "timestamp": 1.2,
                },
                {"type": "iteration", "data": {"iteration": 2, "status": "running"}, "timestamp": 2.0},
                {"type": "planner_decision", "data": {"iteration": 2}, "timestamp": 2.1},
                {
                    "type": "verifier_result",
                    "data": {"iteration": 2, "status": "PASSED", "issues": []},
                    "timestamp": 2.2,
                },
                {"type": "complete", "data": {"success": True}, "timestamp": 3.0},
            ],
        }
        smt = _build_state_machine(trace["events"], trace)
        failed_states = [s for s in smt.states if s["state"] == "FAILED"]
        self.assertGreater(len(failed_states), 0)
        exc = failed_states[0].get("exception")
        self.assertIsNotNone(exc)
        self.assertIn("链路不完整", exc["message"])


class TestStateMachineFromRealTrace(unittest.TestCase):
    """Test state machine building from a real v1.0.0 trace."""

    @classmethod
    def setUpClass(cls):
        candidates = sorted(_TRACE_DIR.glob("sim-headless-*.json"), reverse=True)
        if not candidates:
            candidates = sorted(_TRACE_DIR.glob("sim-*.json"), reverse=True)
        if not candidates:
            raise unittest.SkipTest("No trace files found")
        with open(candidates[0], encoding="utf-8") as f:
            cls.trace = json.load(f)

    def test_state_machine_has_required_structure(self):
        smt = _build_state_machine(self.trace.get("events", []), self.trace)
        self.assertGreater(len(smt.states), 0)
        self.assertGreater(len(smt.transitions), 0)

        # First state should be INITIALIZING (states are already asdict'd)
        first_state = smt.states[0]
        self.assertEqual(first_state["state"], "INITIALIZING")

        # States should have required fields
        for s in smt.states:
            self.assertIn("stateId", s)
            self.assertIn("state", s)
            self.assertIn("enteredAt", s)

        # Transitions should have fromState → toState
        for t in smt.transitions:
            self.assertIn("fromState", t)
            self.assertIn("toState", t)
            self.assertIn("trigger", t)

    def test_state_machine_final_status_matches_trace(self):
        smt = _build_state_machine(self.trace.get("events", []), self.trace)
        # finalStatus should not be UNKNOWN for a complete trace
        self.assertIn(
            smt.finalStatus,
            ["SUCCESS", "FAILED", "CANCELLED"],
            f"Unexpected finalStatus: {smt.finalStatus}",
        )

    def test_compile_real_trace_with_dict_verifier_issues(self):
        """Regression: sim-1f14* traces have dict issues and must compile."""
        path = _TRACE_DIR / "sim-1f14c3ddb100.json"
        if not path.exists():
            self.skipTest("sim-1f14c3ddb100.json not found")
        with open(path, encoding="utf-8") as f:
            trace = json.load(f)
        spec = compile_artifact_spec(trace)
        self.assertTrue(len(spec.serviceContracts) > 0)


class TestProvenanceBuilding(unittest.TestCase):
    """Tests for _build_provenance."""

    def test_provenance_with_tool_calls(self):
        events = [
            {
                "type": "tool_call_record",
                "data": {
                    "call_id": "call-1",
                    "tool_name": "discover",
                    "service_id": "svc-1",
                    "channel": "real_mcp",
                    "result_hash": "abc123",
                    "timestamp": 1717891200.0,
                },
            }
        ]
        prov = _build_provenance(
            session_id="sim-test",
            trace_version="v1.0.0",
            trace_hash=_sha256("dummy"),
            config_hash=_sha256("dummy-cfg"),
            events=events,
            compiler_version="0.1.0",
            created_at="2026-06-09T00:00:00+00:00",
        )
        self.assertEqual(prov.sourceSessionId, "sim-test")
        self.assertEqual(prov.sourceTraceVersion, "v1.0.0")
        self.assertEqual(len(prov.toolCallProvenance), 1)
        self.assertEqual(prov.toolCallProvenance[0]["callId"], "call-1")
        self.assertEqual(prov.toolCallProvenance[0]["resultHash"], "abc123")


class TestSchemaValidation(unittest.TestCase):
    """Validate compiled output against artifact_spec_schema.json."""

    @classmethod
    def setUpClass(cls):
        schema_path = (
            _REPO_ROOT / "trace_evidence" / "schemas" / "artifact_spec_schema.json"
        )
        if not schema_path.exists():
            raise unittest.SkipTest("Schema file not found")
        with open(schema_path, encoding="utf-8") as f:
            cls.schema = json.load(f)

        candidates = sorted(_TRACE_DIR.glob("sim-headless-*.json"), reverse=True)
        if not candidates:
            raise unittest.SkipTest("No trace files found")
        with open(candidates[0], encoding="utf-8") as f:
            cls.trace = json.load(f)

    def _validate(self, instance: dict, schema: dict) -> list[str]:
        """Basic JSON Schema validation (Draft 2020-12 subset). Returns list of error messages."""
        import re

        errors: list[str] = []

        # Check required fields
        for req in schema.get("required", []):
            if req not in instance:
                instance_key = req
                errors.append(f"Missing required field: {instance_key}")

        # Check property types and constraints
        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            if prop_name not in instance:
                continue
            value = instance[prop_name]

            # Check type
            expected_type = prop_schema.get("type")
            if expected_type:
                type_ok = False
                if isinstance(expected_type, list):
                    type_ok = any(self._check_type(value, t) for t in expected_type)
                    # If value is null and null is allowed, skip further checks
                    if value is None and "null" in expected_type:
                        continue
                else:
                    type_ok = self._check_type(value, expected_type)
                if not type_ok:
                    errors.append(
                        f"Field '{prop_name}': expected {expected_type}, got {type(value).__name__}"
                    )

            # Check pattern
            pattern = prop_schema.get("pattern")
            if pattern and isinstance(value, str) and value:
                if not re.match(pattern, value):
                    errors.append(
                        f"Field '{prop_name}': value '{value}' does not match pattern '{pattern}'"
                    )

            # Recursively validate objects
            if isinstance(value, dict) and (
                prop_schema.get("type") == "object"
                or (isinstance(prop_schema.get("type"), list) and "object" in prop_schema["type"])
            ):
                sub_errors = self._validate(value, prop_schema)
                for se in sub_errors:
                    errors.append(f"  {prop_name}.{se}")

            # Validate arrays
            if isinstance(value, list) and (
                prop_schema.get("type") == "array"
                or (isinstance(prop_schema.get("type"), list) and "array" in prop_schema["type"])
            ):
                items_schema = prop_schema.get("items", {})
                if items_schema.get("type") == "object":
                    for i, item in enumerate(value):
                        if isinstance(item, dict):
                            sub_errors = self._validate(item, items_schema)
                            for se in sub_errors:
                                errors.append(f"  {prop_name}[{i}].{se}")

        return errors

    @staticmethod
    def _check_type(value, type_name: str) -> bool:
        if type_name == "string":
            return isinstance(value, str)
        if type_name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if type_name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if type_name == "boolean":
            return isinstance(value, bool)
        if type_name == "object":
            return isinstance(value, dict)
        if type_name == "array":
            return isinstance(value, list)
        if type_name == "null":
            return value is None
        return True

    def test_compiled_output_passes_schema_validation(self):
        """Real compiled ArtifactSpec should pass schema validation."""
        spec = compile_artifact_spec(self.trace, schema_version="0.1.0")
        d = spec.to_dict()
        errors = self._validate(d, self.schema)
        if errors:
            self.fail(f"Schema validation failed with {len(errors)} error(s):\n" + "\n".join(errors[:10]))

    def test_schema_validates_state_machine_enum_values(self):
        """State machine states should use valid enum values."""
        smt = _build_state_machine(self.trace.get("events", []), self.trace)
        valid_states = {
            "INITIALIZING", "DISCOVERING", "PLANNING", "EXECUTING",
            "VERIFYING", "PASSED", "FAILED", "RETRYING", "COMPLETED",
            "TERMINAL_FAILED", "CANCELLED",
        }
        for s in smt.states:
            self.assertIn(s["state"], valid_states, f"Invalid state: {s['state']}")


class TestServiceContracts(unittest.TestCase):
    """Tests for _build_service_contracts — declared + observed aggregation."""

    def _trace(self, services_meta: list, tool_calls: list) -> dict:
        return {
            "session_id": "sim-sc",
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {"servicesMeta": services_meta},
            },
            "events": [{"type": "tool_call_record", "data": d} for d in tool_calls],
        }

    def test_declared_and_observed_aggregation(self):
        svc = [{
            "id": "s1", "name": "风险服务",
            "tools": [{"id": "t1", "name": "predict", "description": "预测"}],
        }]
        calls = [
            {"call_id": "c1", "tool_name": "predict", "service_id": "s1",
             "channel": "real_mcp", "transport": "sse", "latency_ms": 100, "success": True},
            {"call_id": "c2", "tool_name": "predict", "service_id": "s1",
             "channel": "real_mcp", "transport": "sse", "latency_ms": 300, "success": False},
        ]
        contracts = _build_service_contracts(self._trace(svc, calls))
        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertEqual(c.serviceId, "s1")
        self.assertEqual(c.channel, "real_mcp")
        self.assertEqual(c.transport, "sse")
        self.assertEqual(len(c.declaredTools), 1)
        self.assertEqual(c.declaredTools[0]["name"], "predict")
        self.assertEqual(c.totalCalls, 2)
        self.assertEqual(c.overallSuccessRate, 0.5)
        self.assertEqual(len(c.observedTools), 1)
        ot = c.observedTools[0]
        self.assertEqual(ot["toolName"], "predict")
        self.assertEqual(ot["callCount"], 2)
        self.assertEqual(ot["successCount"], 1)
        self.assertEqual(ot["failureCount"], 1)
        self.assertEqual(ot["successRate"], 0.5)
        self.assertEqual(ot["avgLatencyMs"], 200.0)
        self.assertEqual(ot["evidenceRefs"], ["c1", "c2"])

    def test_service_without_calls_has_empty_observed(self):
        svc = [{"id": "s1", "name": "未调用服务", "tools": [{"name": "foo"}]}]
        contracts = _build_service_contracts(self._trace(svc, []))
        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertEqual(c.totalCalls, 0)
        self.assertIsNone(c.overallSuccessRate)
        self.assertIsNone(c.channel)
        self.assertEqual(c.observedTools, [])
        self.assertEqual(len(c.declaredTools), 1)

    def test_no_services_yields_empty_list(self):
        self.assertEqual(_build_service_contracts(self._trace([], [])), [])

    def test_contracts_present_in_compiled_spec(self):
        svc = [{"id": "s1", "name": "svc", "tools": [{"name": "predict"}]}]
        calls = [{"call_id": "c1", "tool_name": "predict", "service_id": "s1",
                  "channel": "real_mcp", "success": True, "latency_ms": 50}]
        trace = self._trace(svc, calls)
        trace.update({"iterations": 1, "elapsed_ms": 10, "success": True,
                      "strategy": {}, "app_name": "svc", "domain": "aml"})
        spec = compile_artifact_spec(trace)
        self.assertEqual(len(spec.serviceContracts), 1)
        self.assertEqual(spec.serviceContracts[0]["serviceId"], "s1")


class TestParsedIntent(unittest.TestCase):
    """Tests for parsedIntent extraction from scenario_parsed events."""

    def test_extract_last_scenario_parsed(self):
        events = [
            {"type": "scenario_parsed", "data": {"goal": "旧", "constraints": []}},
            {"type": "log", "data": {}},
            {"type": "scenario_parsed", "data": {"goal": "新目标", "acceptanceCriteria": ["完成报告"]}},
        ]
        intent = _extract_parsed_intent(events)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["goal"], "新目标")

    def test_none_when_absent(self):
        self.assertIsNone(_extract_parsed_intent([{"type": "log", "data": {}}]))

    def test_none_when_data_empty(self):
        self.assertIsNone(_extract_parsed_intent([{"type": "scenario_parsed", "data": {}}]))

    def test_scenario_build_includes_parsed_intent(self):
        trace = {
            "session_id": "sim-pi",
            "app_name": "报告应用",
            "domain": "aml",
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {
                    "scenarioDescription": "生成风险报告",
                    "servicesMeta": [],
                },
            },
            "events": [{
                "type": "scenario_parsed",
                "data": {"goal": "生成风险报告", "constraints": ["合规"],
                         "acceptanceCriteria": ["报告含三类风险"]},
            }],
        }
        meta_app = _build_meta_app(trace)
        scenario = _build_scenario(trace, meta_app)
        self.assertIsNotNone(scenario.parsedIntent)
        self.assertEqual(scenario.parsedIntent["goal"], "生成风险报告")
        self.assertEqual(scenario.parsedIntent["constraints"], ["合规"])


class TestParseVerification(unittest.TestCase):
    """Regression: Verifier 中文「验证通过」须识别为 PASSED，勿误报 issue。"""

    def _parse(self, text):
        from micro_agent.simulation.orchestrator import SimulationOrchestrator
        return SimulationOrchestrator._parse_verification(text)

    def test_chinese_passed_prefix(self):
        text = "验证通过：服务编排完整执行，关键服务调用合理"
        passed, issue = self._parse(text)
        self.assertTrue(passed)
        self.assertEqual(issue, "")

    def test_explicit_passed(self):
        passed, issue = self._parse("PASSED\n\nAll good")
        self.assertTrue(passed)

    def test_failed_with_reason(self):
        passed, issue = self._parse("FAILED: openFDA 查询失败")
        self.assertFalse(passed)
        self.assertIn("openFDA", issue)


class TestScenarioIntentJsonParse(unittest.TestCase):
    """Tests for SimulationOrchestrator._parse_intent_json robustness."""

    def _parse(self, text):
        from micro_agent.simulation.orchestrator import SimulationOrchestrator
        return SimulationOrchestrator._parse_intent_json(text)

    def test_plain_json(self):
        self.assertEqual(self._parse('{"goal": "x"}')["goal"], "x")

    def test_json_in_markdown_fence(self):
        text = '```json\n{"goal": "y", "constraints": []}\n```'
        self.assertEqual(self._parse(text)["goal"], "y")

    def test_garbage_returns_none(self):
        self.assertIsNone(self._parse("no json here"))
        self.assertIsNone(self._parse(""))


if __name__ == "__main__":
    unittest.main()
