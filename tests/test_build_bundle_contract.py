"""Stable Artifact and BuildBundle contracts used by CI."""

import tempfile
from pathlib import Path

from micro_agent.simulation.artifact_compiler import (
    build_accepted_trajectory,
    build_meta_app_artifact,
    stable_hash,
)
from micro_agent.simulation.build_bundle import BuildBundleStore

from fixtures.golden import (
    GOLDEN_ARTIFACT_HASH,
    GOLDEN_ARTIFACT_ID,
    load_golden_artifact,
)


def _trace():
    return {
        "schemaVersion": "build_trace.v1",
        "build_id": "build-test",
        "session_id": "build-test",
        "app_name": "用药辅助",
        "domain": "health",
        "success": True,
        "cancelled": False,
        "terminalStatus": "SUCCEEDED",
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
                        "mcpUrl": "http://fdueblab.cn:25013/sse",
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
                    "result": '{"dose":"600mg q12h"}',
                    "latency_ms": 20,
                    "timestamp": 1.0,
                    "success": True,
                    "error": None,
                },
            },
            {
                "type": "verifier_result",
                "data": {"iteration": 1, "status": "PASSED", "summary": "业务目标满足"},
            },
        ],
    }


def _compile(trace):
    return build_meta_app_artifact(trace, build_accepted_trajectory(trace))


def test_artifact_is_minimal_and_has_explicit_fake_contract():
    artifact = _compile(_trace())

    assert artifact["schemaVersion"] == "meta_app_artifact.v1"
    assert set(artifact) == {
        "schemaVersion",
        "artifactId",
        "app",
        "taskContract",
        "runtime",
        "goldenPaths",
    }
    assert artifact["runtime"]["serviceBindings"][0]["isFake"] is False


def test_accepted_trajectory_generates_primary_golden_path():
    trace = _trace()
    accepted = build_accepted_trajectory(trace)
    artifact = build_meta_app_artifact(trace, accepted)

    assert accepted["schemaVersion"] == "accepted_trajectory.v1"
    assert accepted["status"] == "accepted"
    assert len(accepted["actionSequence"]) == 1
    assert artifact["taskContract"]["inputSlots"] == [
        {"name": "weight_kg", "type": "integer", "required": True},
        {"name": "renal_function", "type": "string", "required": True},
    ]
    assert len(artifact["goldenPaths"]) == 1
    assert artifact["goldenPaths"][0]["primary"] is True


def test_failed_final_iteration_does_not_accept_earlier_pass():
    trace = _trace()
    trace["success"] = False
    trace["iterations"] = 2
    trace["events"].append(
        {
            "type": "verifier_result",
            "data": {"iteration": 2, "status": "FAILED", "summary": "最终轮未通过"},
        }
    )

    accepted = build_accepted_trajectory(trace)
    artifact = build_meta_app_artifact(trace, accepted)
    assert accepted["status"] == "missing"
    assert accepted["acceptedIteration"] is None
    assert accepted["actionSequence"] == []
    assert artifact["runtime"]["mode"] == "agent_only"
    assert artifact["goldenPaths"] == []


def test_artifact_boundary_includes_all_recommended_bindings():
    trace = _trace()
    trace["metadata"]["config_snapshot"]["servicesMeta"].append(
        {
            "id": "unused",
            "name": "Fallback Service",
            "isFake": False,
            "mcpMethod": "sse",
            "mcpUrl": "https://example.test/sse",
        }
    )
    artifact = _compile(trace)
    assert {row["serviceId"] for row in artifact["runtime"]["serviceBindings"]} == {
        "linezolid",
        "unused",
    }
    assert {step["serviceId"] for step in artifact["goldenPaths"][0]["steps"]} == {"linezolid"}


def test_artifact_id_changes_with_executable_path():
    first = _trace()
    second = _trace()
    second["events"][1]["data"]["arguments"]["weight_kg"] = 80
    assert _compile(first)["artifactId"] != _compile(second)["artifactId"]


def test_canonical_trace_is_cross_tier_golden_source_of_truth():
    """MA 是黄金 Artifact 真源；三端共享同一份 fixture 与 id/hash 常量。"""
    artifact = _compile(_trace())

    assert artifact == load_golden_artifact()
    assert artifact["artifactId"] == GOLDEN_ARTIFACT_ID
    assert stable_hash(artifact) == GOLDEN_ARTIFACT_HASH


def test_bundle_manifest_points_to_artifact_without_reverse_reference():
    with tempfile.TemporaryDirectory() as tmp:
        store = BuildBundleStore(Path(tmp))
        manifest = store.save_from_trace(_trace())
        artifact = store.load_part("build-test", "artifact")
        accepted = store.load_part("build-test", "accepted_trajectory")

    assert manifest["artifactHash"] == accepted["generatedArtifact"]["artifactHash"]
    assert manifest["publishable"] is True
    assert accepted["generatedArtifact"]["artifactId"] == artifact["artifactId"]
    assert "sourceTraceId" not in artifact
    assert "acceptedTrajectoryRef" not in artifact
