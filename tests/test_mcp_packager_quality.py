"""Tests for AMQ-compatible scoring and leakage-aware suite selection."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_packager.amq_suite import prepare_amq_suite
from mcp_packager.quality import aggregate_quality, score_verification


def _runtime_report(*, case_success: bool = True) -> dict:
    return {
        "verificationVersion": "ioeb.mcp-runtime-verification/v1",
        "success": case_success,
        "checks": [
            {"name": "initialize", "success": True},
            {"name": "tools/list", "success": True},
        ],
        "tools": [
            {
                "name": "main_process",
                "description": "Repeat text and return a structured output.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Text to repeat.",
                        },
                        "repeat": {
                            "type": "integer",
                            "description": "Number of repetitions.",
                            "default": 1,
                        },
                    },
                    "required": ["text"],
                },
                "outputSchema": {"type": "object"},
            }
        ],
        "cases": [
            {
                "name": "case-1",
                "success": case_success,
                "mcp": {"value": "IoEBIoEB"},
                "differentialMatch": case_success,
                "expectedMatch": case_success,
            }
        ],
        "probes": [
            {
                "name": "missing-required",
                "parameter": "text",
                "handled": True,
                "specific": True,
            },
            {
                "name": "type-mismatch",
                "parameter": "repeat",
                "handled": True,
                "specific": True,
            },
        ],
    }


def test_runtime_scoring_reports_provisional_quality_without_fake_docker_score() -> None:
    quality = score_verification(_runtime_report())

    assert quality["d1Availability"]["score"] is None
    assert quality["d1Availability"]["healthSuccess"] is True
    assert quality["d2Usability"]["score"] > 0.8
    assert quality["d2Usability"]["goe"]["paramDescCoverage"] == 1.0
    assert quality["d2Usability"]["gov"]["errorSpecificity"] == 1.0
    assert quality["d3Utility"]["score"] == 1.0
    assert quality["aqs"] is None
    assert quality["provisionalQualityWithoutBuild"] is not None
    assert quality["publishable"] is False


def test_single_literal_const_counts_as_type_and_constraint_support() -> None:
    runtime = _runtime_report()
    runtime["tools"][0]["inputSchema"] = {
        "type": "object",
        "properties": {
            "system": {
                "const": "mass_spring_damper",
                "description": "Supported system.",
            }
        },
        "required": ["system"],
    }

    goe = score_verification(runtime)["d2Usability"]["goe"]

    assert goe["paramTypeCoverage"] == 1.0
    assert goe["constraintRichness"] == 1.0


def test_docker_scoring_applies_availability_gate_and_failure_attribution() -> None:
    verification = {
        "verificationVersion": "ioeb.mcp-artifact-verification/v1",
        "mode": "docker",
        "success": True,
        "checks": [
            {"name": "docker:daemon", "success": True},
            {"name": "docker:build", "success": True},
            {"name": "docker:start", "success": True},
        ],
        "runtime": _runtime_report(),
    }
    quality = score_verification(verification)

    assert quality["d1Availability"]["score"] == 1.0
    assert quality["d3Utility"]["score"] == 1.0
    assert quality["aqs"] is not None
    assert quality["publishable"] is True
    assert quality["failureCategory"] is None

    verification["runtime"] = _runtime_report(case_success=False)
    failed = score_verification(verification)
    assert failed["aqs"] < quality["aqs"]
    assert failed["failureCategory"] == "functional_mismatch"


def test_publish_gate_rejects_unspecific_or_unhandled_invalid_input() -> None:
    runtime = _runtime_report()
    runtime["probes"][0]["specific"] = False
    verification = {
        "mode": "docker",
        "checks": [{"name": "docker:build", "success": True}],
        "runtime": runtime,
    }

    quality = score_verification(verification)

    assert quality["d1Availability"]["score"] == 1.0
    assert quality["d3Utility"]["score"] == 1.0
    assert quality["inputValidationGate"] == {
        "passed": False,
        "passedProbes": 1,
        "totalProbes": 2,
    }
    assert quality["publishable"] is False
    assert quality["qualityGatePassed"] is False
    assert quality["failureCategory"] == "input_validation_failure"


def test_runtime_start_failures_distinguish_imports_and_timeouts() -> None:
    base = {
        "mode": "docker",
        "checks": [{"name": "docker:build", "success": True}],
    }
    import_failure = {
        **base,
        "containerLogTail": "ModuleNotFoundError: No module named 'missing_pkg'",
    }
    timeout_failure = {
        **base,
        "error": {"type": "TimeoutExpired", "message": "command timed out"},
    }

    assert score_verification(import_failure)["failureCategory"] == "import_error"
    assert score_verification(timeout_failure)["failureCategory"] == "runtime_timeout"


def test_aggregate_quality_exposes_system_level_indicators() -> None:
    passed_verification = {
        "mode": "docker",
        "checks": [{"name": "docker:build", "success": True}],
        "runtime": _runtime_report(),
    }
    failed_verification = {
        "mode": "docker",
        "checks": [{"name": "docker:build", "success": True}],
        "runtime": _runtime_report(case_success=False),
    }
    aggregate = aggregate_quality(
        [score_verification(passed_verification), score_verification(failed_verification)]
    )

    assert aggregate["samples"] == 2
    assert aggregate["utilityPassRate"] == 0.5
    assert aggregate["publishableRate"] == 0.5
    assert aggregate["inputValidationGatePassRate"] == 1.0
    assert aggregate["failureCategories"] == {"functional_mismatch": 1}


def test_prepare_amq_suite_filters_weak_samples_and_prevents_repo_leakage(tmp_path: Path) -> None:
    dataset = tmp_path / "amq.jsonl"
    status = tmp_path / "status.json"
    dev_ids = tmp_path / "dev.json"
    rows = [
        {
            "sample_id": "sample-dev",
            "tags": ["self_built"],
            "required_env": [],
            "repo_info": {"url": "https://example/repo-a", "commit_sha": "a", "category": "L1_Library"},
            "wrap_intent": "dev intent",
            "difficulty": "easy",
        },
        {
            "sample_id": "sample-same-repo",
            "tags": ["self_built"],
            "required_env": [],
            "repo_info": {"url": "https://example/repo-a", "commit_sha": "a", "category": "L2_Script"},
            "wrap_intent": "same repository",
            "difficulty": "medium",
        },
        {
            "sample_id": "sample-holdout",
            "tags": ["scienceagentbench"],
            "required_env": [],
            "repo_info": {"url": "https://example/repo-b", "commit_sha": "b", "category": "L3_Complex"},
            "wrap_intent": "holdout intent",
            "difficulty": "hard",
        },
        {
            "sample_id": "sample-env",
            "tags": ["toolarena"],
            "required_env": [{"name": "TOKEN"}],
            "repo_info": {"url": "https://example/repo-c", "commit_sha": "c", "category": "L1_Library"},
            "wrap_intent": "secret dependent",
        },
        {
            "sample_id": "sample-weak",
            "tags": ["self_built"],
            "required_env": [],
            "repo_info": {"url": "https://example/repo-d", "commit_sha": "d", "category": "L1_Library"},
            "wrap_intent": "weak oracle",
        },
        {
            "sample_id": "sample-negative",
            "tags": ["self_built"],
            "required_env": [],
            "repo_info": {"url": "https://example/repo-e", "commit_sha": "e", "category": "L0_Infeasible"},
            "wrap_intent": "infeasible",
        },
        {
            "sample_id": "meb_l0_mislabeled_001",
            "tags": ["self_built"],
            "required_env": [],
            "repo_info": {"url": "https://example/repo-f", "commit_sha": "f", "category": "L1_Library"},
            "wrap_intent": "mislabeled negative control",
        },
    ]
    dataset.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    status.write_text(
        json.dumps(
            {
                "sample-dev": {"verify_tier": "specific_numeric"},
                "sample-same-repo": {"verify_tier": "domain_structural"},
                "sample-holdout": {"verify_tier": "exact_string"},
                "sample-env": {"verify_tier": "specific_numeric"},
                "sample-weak": {"verify_tier": "keyword_check"},
                "sample-negative": {"verify_tier": "keyword_check"},
                "meb_l0_mislabeled_001": {"verify_tier": "specific_numeric"},
            }
        ),
        encoding="utf-8",
    )
    dev_ids.write_text(json.dumps(["sample-dev"]), encoding="utf-8")

    suite = prepare_amq_suite(dataset, status, development_ids=dev_ids)

    assert suite["summary"] == {
        "sourceSamples": 7,
        "development": 2,
        "holdout": 1,
        "negative": 2,
        "excluded": 2,
        "excludedReasons": {
            "requires_external_environment": 1,
            "weak_or_unknown_oracle": 1,
        },
    }
    assert {item["sampleId"] for item in suite["splits"]["development"]} == {
        "sample-dev",
        "sample-same-repo",
    }
    assert all(item["requiresManualAudit"] for item in suite["splits"]["negative"])
