import tempfile
import unittest
from pathlib import Path

from micro_agent.simulation.artifact_compiler import compile_build
from micro_agent.simulation.build_bundle import BuildBundleStore


def _trace():
    return {
        "schemaVersion": "build_trace.v1",
        "build_id": "build-test",
        "session_id": "build-test",
        "app_name": "用药辅助",
        "domain": "health",
        "mode": "production",
        "strategy": {},
        "success": True,
        "iterations": 1,
        "elapsed_ms": 100,
        "metadata": {
            "trace_version": "build_trace.v1",
            "config_snapshot": {
                "appName": "用药辅助",
                "domain": "health",
                "scenarioDescription": "根据患者体重和肾功能给出用药建议",
                "servicesMeta": [
                    {
                        "id": "linezolid",
                        "name": "Linezolid",
                        "isFake": False,
                        "mcpMethod": "sse",
                        "mcpUrl": "http://127.0.0.1:25013/sse",
                        "tools": [{"name": "calculate_dose", "description": "dose"}],
                    }
                ],
            },
        },
        "events": [
            {
                "type": "scenario_parsed",
                "data": {
                    "goal": "给出用药建议",
                    "description": "根据患者体重和肾功能给出用药建议",
                    "constraints": ["使用已绑定服务"],
                    "acceptanceCriteria": ["输出剂量建议"],
                    "domain": "health",
                },
            },
            {
                "type": "service_selection",
                "data": {
                    "schemaVersion": "service_selection_report.v1",
                    "selectionId": "sel-test",
                    "strategy": "llm_catalog_selection",
                    "selectedServices": [
                        {
                            "serviceId": "linezolid",
                            "serviceName": "Linezolid",
                            "reason": "dose task",
                            "matchedCapabilities": ["calculate_dose"],
                        }
                    ],
                    "rejectedServices": [],
                    "missingCapabilities": [],
                    "rationale": "selected dose service",
                },
            },
            {
                "type": "tool_call_record",
                "data": {
                    "call_id": "call-1",
                    "tool_name": "linezolid_calculate_dose",
                    "service_id": "linezolid",
                    "service_name": "Linezolid",
                    "channel": "real_mcp",
                    "source": "real_mcp",
                    "transport": "sse",
                    "phase": "slow_mode",
                    "purpose": "react_action",
                    "iteration": 1,
                    "action_id": "iter1-a1",
                    "arguments": {"weight_kg": 70, "renal_function": "normal"},
                    "result": "{\"dose\":\"600mg q12h\"}",
                    "latency_ms": 20,
                    "timestamp": 1.0,
                    "success": True,
                    "error": None,
                },
            },
            {
                "type": "verifier_result",
                "data": {
                    "iteration": 1,
                    "status": "PASSED",
                    "summary": "业务目标满足",
                },
            },
        ],
    }


class SimulationBuildBundleTest(unittest.TestCase):
    def test_artifact_is_minimal_and_excludes_build_diagnostics(self):
        compiled = compile_build(_trace())
        artifact = compiled.artifact

        self.assertEqual(artifact["schemaVersion"], "meta_app_artifact.v1")
        self.assertIn("taskContract", artifact)
        self.assertIn("runtime", artifact)
        self.assertIn("goldenPaths", artifact)
        self.assertNotIn("serviceSelection", artifact)
        self.assertNotIn("solidificationReport", artifact)
        self.assertNotIn("parsedIntent", artifact)
        self.assertNotIn("provenance", artifact)
        self.assertNotIn("artifactHash", artifact)

    def test_accepted_trajectory_is_separate_and_generates_primary_golden_path(self):
        compiled = compile_build(_trace())
        accepted = compiled.acceptedTrajectory
        artifact = compiled.artifact

        self.assertEqual(accepted["schemaVersion"], "accepted_trajectory.v1")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(len(accepted["actionSequence"]), 1)
        self.assertEqual(len(artifact["goldenPaths"]), 1)
        self.assertTrue(artifact["goldenPaths"][0]["primary"])

    def test_bundle_manifest_points_to_artifact_without_artifact_pointing_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BuildBundleStore(Path(tmp))
            manifest = store.save_from_trace(_trace())
            artifact = store.load_part("build-test", "artifact")
            accepted = store.load_part("build-test", "accepted_trajectory")

        self.assertIn("artifact", manifest["hashes"])
        self.assertEqual(accepted["generatedArtifact"]["artifactId"], artifact["artifactId"])
        self.assertNotIn("sourceTraceId", artifact)
        self.assertNotIn("acceptedTrajectoryRef", artifact)


if __name__ == "__main__":
    unittest.main()
