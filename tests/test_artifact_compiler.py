"""Tests for artifact_compiler v0.3 — goldenPath / solidification convergence."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from micro_agent.simulation.artifact_compiler import (
    ArtifactSpec,
    MetaAppInfo,
    _TraceSummary,
    _build_evidence,
    _build_execution_trace,
    _build_meta_app,
    _build_parsed_intent,
    _build_provenance,
    _build_scenario,
    _build_service_contracts,
    _build_solidification_report,
    _build_write_back,
    _extract_golden_path,
    _sha256,
    _short_id,
    _ts_to_iso,
    compile_artifact_spec,
)
from micro_agent.scenario.schema import normalize_scenario_parsed

_TRACE_DIR = _REPO_ROOT / "workspace" / "data" / "traces"


def _evidence_complete() -> MagicMock:
    ev = MagicMock()
    ev.evidenceId = "ev-test-001"
    ev.completeness = "COMPLETE"
    ev.checkerStatus = "PASS"
    ev.missingEvidenceCategories = []
    return ev


def _pipeline_result(ev: MagicMock) -> MagicMock:
    pr = MagicMock()
    pr.card.evidence_id = ev.evidenceId
    pr.report.overall_status = ev.checkerStatus
    pr.report.completeness = ev.completeness
    pr.bundle.missing_evidence = ev.missingEvidenceCategories
    return pr


def _planner_decision(tool_details, execution_path=None):
    return {
        "iteration": 1,
        "selected_tools": [d.get("tool") for d in tool_details],
        "executionPath": execution_path or ["用户输入"] + [d.get("tool") for d in tool_details] + ["输出结果"],
        "tool_call_details": tool_details,
    }


def _success_real_call(call_id, tool, service_id="s1", arguments=None):
    return {
        "call_id": call_id,
        "tool": tool,
        "service": service_id,
        "channel": "real_mcp",
        "success": True,
        "arguments": arguments or {"query": "test"},
        "result_preview": '{"score": 3}',
        "latency_ms": 100,
    }


class TestUtilityFunctions(unittest.TestCase):
    def test_sha256_deterministic(self):
        self.assertEqual(_sha256("hello"), _sha256("hello"))
        self.assertEqual(len(_sha256("hello")), 64)

    def test_short_id_format(self):
        self.assertRegex(_short_id("art", "seed"), r"^art-[a-f0-9]{6}-[a-f0-9]{8}$")

    def test_ts_to_iso_none(self):
        self.assertEqual(_ts_to_iso(None), "")


class TestParsedIntent(unittest.TestCase):
    def test_no_intake_dialogue_in_artifact(self):
        trace = {
            "session_id": "sim-intent",
            "metadata": {
                "config_snapshot": {"scenarioDescription": "测试"},
                "runtime": {"trace_version": "v1.0.0"},
            },
            "events": [{
                "type": "scenario_parsed",
                "data": {
                    "goal": "目标",
                    "description": "描述",
                    "source": {
                        "rawUserInput": "原始",
                        "intakeDialogue": [{"role": "user", "content": "长对话"}],
                        "intakeSessionId": "intake-1",
                    },
                },
            }],
            "iterations": 1,
            "elapsed_ms": 100,
        }
        ma = MetaAppInfo(appName="App", domain="health")
        intent = _build_parsed_intent(trace, ma, "sim-intent")
        self.assertEqual(intent["goal"], "目标")
        self.assertIn("sourceRef", intent)
        self.assertEqual(intent["sourceRef"]["intakeSessionRef"], "intake-1")
        self.assertNotIn("intakeDialogue", intent)
        self.assertNotIn("source", intent)


class TestSolidificationGates(unittest.TestCase):
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
                {"type": "tool_call_record", "data": {
                    "call_id": "c1", "channel": "real_mcp", "success": True,
                    "tool_name": "predict", "service_id": "s1",
                }},
                {"type": "verifier_result", "data": {"status": "PASSED", "iteration": 1}},
            ]
        )
        summary = _TraceSummary(finalStatus="SUCCESS", totalIterations=2, elapsedMs=5000)
        report = _build_solidification_report(trace, summary, _evidence_complete())
        self.assertTrue(report.solidifiable)

    def test_mock_demo_not_solidifiable(self):
        trace = self._make_trace(
            events=[
                {"type": "tool_call_record", "data": {
                    "call_id": "c1", "channel": "sandbox", "success": True,
                }},
                {"type": "verifier_result", "data": {"status": "PASSED"}},
            ]
        )
        summary = _TraceSummary(finalStatus="SUCCESS")
        report = _build_solidification_report(trace, summary, _evidence_complete())
        self.assertFalse(report.solidifiable)
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertIsNone(spec.goldenPath)
        self.assertFalse(spec.solidificationReport["goldenPathExtractable"])

    def test_evidence_pipeline_failure(self):
        trace = self._make_trace(
            events=[
                {"type": "tool_call_record", "data": {
                    "call_id": "c1", "channel": "real_mcp", "success": True,
                }},
                {"type": "verifier_result", "data": {"status": "PASSED"}},
            ]
        )
        summary = _TraceSummary(finalStatus="SUCCESS")
        report = _build_solidification_report(trace, summary, None)
        self.assertFalse(report.solidifiable)
        spec = compile_artifact_spec(trace, None)
        self.assertIsNone(spec.goldenPath)
        self.assertIn("运行证据 pipeline", " ".join(spec.solidificationReport["remediation"]))

    def test_tool_call_failure_not_solidifiable(self):
        trace = self._make_trace(
            events=[
                {"type": "tool_call_record", "data": {
                    "call_id": "c1", "channel": "real_mcp", "success": False,
                }},
                {"type": "verifier_result", "data": {"status": "PASSED"}},
            ]
        )
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertFalse(spec.solidifiable)
        self.assertIsNone(spec.goldenPath)

    def test_verifier_final_failed(self):
        trace = self._make_trace(
            events=[
                {"type": "tool_call_record", "data": {
                    "call_id": "c1", "channel": "real_mcp", "success": True,
                }},
                {"type": "verifier_result", "data": {"status": "FAILED"}},
            ]
        )
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertFalse(spec.solidifiable)
        self.assertIsNone(spec.goldenPath)


class TestGoldenPathExtraction(unittest.TestCase):
    def _golden_trace(self, planner=None, extra_events=None):
        planner = planner or _planner_decision([
            _success_real_call("c1", "predict", arguments={"patient": "p1"}),
            _success_real_call("c2", "report", service_id="s2", arguments={"from_step": "c1"}),
        ])
        events = [
            {"type": "scenario_parsed", "data": {
                "goal": "生成报告",
                "acceptanceCriteria": ["含风险评分"],
                "constraints": ["合规"],
            }},
            {"type": "tool_call_record", "data": {
                "call_id": "c1", "tool_name": "predict", "service_id": "s1",
                "channel": "real_mcp", "success": True,
            }},
            {"type": "tool_call_record", "data": {
                "call_id": "c2", "tool_name": "report", "service_id": "s2",
                "channel": "real_mcp", "success": True,
            }},
            {"type": "verifier_result", "data": {
                "status": "PASSED",
                "iteration": 1,
                "plannerDecision": planner,
            }},
            {"type": "complete", "data": {"success": True}},
        ]
        if extra_events:
            events = extra_events + events
        return {
            "session_id": "sim-golden",
            "iterations": 1,
            "elapsed_ms": 3000,
            "strategy": {"minIterations": 1},
            "app_name": "测试",
            "domain": "health",
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {
                    "scenarioDescription": "生成报告",
                    "servicesMeta": [
                        {"id": "s1", "name": "预测", "tools": [{"name": "predict"}]},
                        {"id": "s2", "name": "报告", "tools": [{"name": "report"}]},
                    ],
                },
            },
            "events": events,
        }

    def test_full_success_extracts_golden_path(self):
        trace = self._golden_trace()
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertTrue(spec.solidifiable)
        self.assertTrue(spec.solidificationReport["goldenPathExtractable"])
        self.assertIsNotNone(spec.goldenPath)
        self.assertEqual(len(spec.goldenPath["steps"]), 2)
        self.assertNotIn("executionTrace", spec.to_dict())
        step_tools = [s["toolName"] for s in spec.goldenPath["steps"]]
        self.assertEqual(step_tools, ["predict", "report"])
        self.assertGreater(len(spec.goldenPath["assertions"]), 0)
        self.assertIn("applicability", spec.goldenPath)
        self.assertIn("fallbackPolicy", spec.goldenPath)

    def test_tool_call_failure_blocks_solidification(self):
        trace = self._golden_trace()
        trace["events"].insert(3, {
            "type": "tool_call_record",
            "data": {
                "call_id": "bad", "tool_name": "predict", "service_id": "s1",
                "channel": "real_mcp", "success": False,
            },
        })
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertFalse(spec.solidifiable)
        self.assertIsNone(spec.goldenPath)

        trace = self._golden_trace()
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        for step in spec.goldenPath["steps"]:
            self.assertIn("stepId", step)
            self.assertIn("inputBinding", step)

    def test_solidifiable_but_missing_binding_not_extractable(self):
        planner = _planner_decision([
            {
                "call_id": "c1",
                "tool": "predict",
                "service": "s1",
                "channel": "real_mcp",
                "success": True,
                "arguments": {"_internal_only": "hidden"},
                "result_preview": "{}",
            },
        ])
        trace = self._golden_trace(planner=planner)
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertTrue(spec.solidifiable)
        self.assertFalse(spec.solidificationReport["goldenPathExtractable"])
        self.assertIsNone(spec.goldenPath)
        self.assertTrue(spec.solidificationReport["remediation"])

    def test_repair_loop_then_success_is_solidifiable(self):
        """前轮失败 + 同工具后轮成功（修复循环）→ 视为已解决，可固化。"""
        planner = _planner_decision([
            _success_real_call("c2", "predict", arguments={"patient": "p1"}),
        ])
        trace = self._golden_trace(planner=planner)
        # 在成功调用前插入同一 (service, tool) 的失败调用：应被后续成功覆盖
        trace["events"].insert(1, {
            "type": "tool_call_record",
            "data": {
                "call_id": "c0", "tool_name": "predict", "service_id": "s1",
                "channel": "real_mcp", "success": False,
            },
        })
        # 把原 golden trace 的两条 record 收敛为一条成功 predict
        trace["events"] = [
            e for e in trace["events"]
            if not (e.get("type") == "tool_call_record"
                    and e["data"].get("call_id") in ("c1", "c2")
                    and e["data"].get("tool_name") == "report")
        ]
        trace["events"].insert(2, {
            "type": "tool_call_record",
            "data": {
                "call_id": "c2", "tool_name": "predict", "service_id": "s1",
                "channel": "real_mcp", "success": True,
            },
        })
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        cond = spec.solidificationReport["conditions"]["noUnresolvedToolErrors"]
        self.assertEqual(cond["unresolvedFailures"], 0)
        self.assertEqual(cond["totalFailures"], 1)
        self.assertEqual(cond["resolvedByRetry"], 1)
        self.assertTrue(spec.solidifiable)

    def test_unresolved_failure_blocks_solidification(self):
        """失败调用没有同工具的后续成功 → 未解决 → 不可固化。"""
        trace = self._golden_trace()
        trace["events"].insert(1, {
            "type": "tool_call_record",
            "data": {
                "call_id": "bad", "tool_name": "lookup", "service_id": "s3",
                "channel": "real_mcp", "success": False,
            },
        })
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        cond = spec.solidificationReport["conditions"]["noUnresolvedToolErrors"]
        self.assertEqual(cond["unresolvedFailures"], 1)
        self.assertFalse(spec.solidifiable)
        self.assertIsNone(spec.goldenPath)

    def test_required_services_from_contract_ref(self):
        trace = self._golden_trace()
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        req = spec.goldenPath["applicability"]["requiredServices"]
        self.assertEqual(req, ["s1", "s2"])
        self.assertNotIn("", req)

    def test_assertions_reflect_reality(self):
        trace = self._golden_trace()
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        by_id = {a["assertionId"]: a for a in spec.goldenPath["assertions"]}
        # forbidden 断言存在且在纯 real_mcp 主干上通过
        self.assertIn("a_l1_forbidden_tools", by_id)
        self.assertEqual(by_id["a_l1_forbidden_tools"]["result"], "pass")
        # tool_order 真正基于 executionPath 子序列判定
        self.assertEqual(by_id["a_l1_tool_order"]["result"], "pass")
        # 所有断言结果只能是合法枚举
        for a in spec.goldenPath["assertions"]:
            self.assertIn(a["result"], ("pass", "fail", "unknown"))

    def test_no_execution_trace_steps_in_artifact(self):
        trace = self._golden_trace()
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        d = spec.to_dict()
        self.assertNotIn("executionTrace", d)
        self.assertNotIn("scenario", d)
        self.assertIn("parsedIntent", d)
        self.assertIn("artifactMeta", d)
        self.assertIn("buildSummary", d["artifactMeta"])
        # goldenPath 步骤不得泄漏内部字段
        for step in d["goldenPath"]["steps"]:
            self.assertNotIn("_callId", step)

    def test_provenance_no_tool_call_list(self):
        trace = self._golden_trace()
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertNotIn("provenance", spec.to_dict())
        self.assertNotIn("toolCallProvenance", spec.artifactMeta)
        meta = spec.artifactMeta
        self.assertEqual(len(meta["artifactHash"]), 64)
        self.assertEqual(meta["traceRef"], "sim-golden")


class TestServiceContracts(unittest.TestCase):
    def test_declared_and_observed_aggregation(self):
        svc = [{"id": "s1", "name": "风险服务", "tools": [{"name": "predict"}]}]
        calls = [
            {"call_id": "c1", "tool_name": "predict", "service_id": "s1",
             "channel": "real_mcp", "success": True, "latency_ms": 100},
            {"call_id": "c2", "tool_name": "predict", "service_id": "s1",
             "channel": "real_mcp", "success": False, "latency_ms": 300},
        ]
        trace = {
            "session_id": "sim-sc",
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {"servicesMeta": svc},
            },
            "events": [{"type": "tool_call_record", "data": d} for d in calls],
        }
        contracts = _build_service_contracts(trace)
        self.assertEqual(contracts[0].totalCalls, 2)


class TestSchemaValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema_path = _REPO_ROOT / "trace_evidence" / "schemas" / "artifact_spec_schema.json"
        with open(schema_path, encoding="utf-8") as f:
            cls.schema = json.load(f)

    def test_golden_spec_passes_schema(self):
        planner = _planner_decision([_success_real_call("c1", "predict")])
        trace = {
            "session_id": "sim-schema",
            "iterations": 1,
            "elapsed_ms": 1000,
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {
                    "scenarioDescription": "x",
                    "servicesMeta": [{"id": "s1", "name": "S", "tools": [{"name": "predict"}]}],
                },
            },
            "events": [
                {"type": "scenario_parsed", "data": {"goal": "x", "domain": "health"}},
                {"type": "tool_call_record", "data": {
                    "call_id": "c1", "tool_name": "predict", "service_id": "s1",
                    "channel": "real_mcp", "success": True,
                }},
                {"type": "verifier_result", "data": {
                    "status": "PASSED", "iteration": 1, "plannerDecision": planner,
                }},
                {"type": "complete", "data": {"success": True}},
            ],
        }
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        errors = self._validate(spec.to_dict(), self.schema)
        if errors:
            self.fail("Schema errors:\n" + "\n".join(errors))

    def _validate(self, instance: dict, schema: dict) -> list[str]:
        """真实 JSON Schema 校验（Draft 2020-12）。"""
        import jsonschema
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        return [f"{list(e.path)}: {e.message}" for e in errors]

    def test_golden_path_passes_full_schema(self):
        planner = _planner_decision([
            _success_real_call("c1", "predict", arguments={"patient": "p1"}),
            _success_real_call("c2", "report", service_id="s2", arguments={"ref": "x"}),
        ])
        trace = {
            "session_id": "sim-schema-gp",
            "iterations": 1,
            "elapsed_ms": 1000,
            "strategy": {"minIterations": 1},
            "metadata": {
                "runtime": {"trace_version": "v1.0.0"},
                "config_snapshot": {
                    "scenarioDescription": "x",
                    "servicesMeta": [
                        {"id": "s1", "name": "S1", "tools": [{"name": "predict"}]},
                        {"id": "s2", "name": "S2", "tools": [{"name": "report"}]},
                    ],
                },
            },
            "events": [
                {"type": "scenario_parsed", "data": {
                    "goal": "x", "domain": "health", "acceptanceCriteria": ["c"],
                }},
                {"type": "tool_call_record", "data": {
                    "call_id": "c1", "tool_name": "predict", "service_id": "s1",
                    "channel": "real_mcp", "success": True,
                }},
                {"type": "tool_call_record", "data": {
                    "call_id": "c2", "tool_name": "report", "service_id": "s2",
                    "channel": "real_mcp", "success": True,
                }},
                {"type": "verifier_result", "data": {
                    "status": "PASSED", "iteration": 1, "plannerDecision": planner,
                }},
                {"type": "complete", "data": {"success": True}},
            ],
        }
        spec = compile_artifact_spec(trace, _pipeline_result(_evidence_complete()))
        self.assertIsNotNone(spec.goldenPath)
        errors = self._validate(spec.to_dict(), self.schema)
        if errors:
            self.fail("Schema errors:\n" + "\n".join(errors))


class TestCompileFromRealTrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidates = sorted(_TRACE_DIR.glob("sim-headless-*.json"), reverse=True)
        if not candidates:
            candidates = sorted(_TRACE_DIR.glob("sim-*.json"), reverse=True)
        if not candidates:
            raise unittest.SkipTest("No trace files found")
        with open(candidates[0], encoding="utf-8") as f:
            cls.trace = json.load(f)

    def test_compile_v03_structure(self):
        spec = compile_artifact_spec(self.trace)
        self.assertEqual(spec.schemaVersion, "0.3.0")
        self.assertIn("parsedIntent", spec.to_dict())
        self.assertNotIn("executionTrace", spec.to_dict())


if __name__ == "__main__":
    unittest.main()
