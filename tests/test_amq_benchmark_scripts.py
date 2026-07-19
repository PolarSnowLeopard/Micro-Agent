from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.run_amq_agentic_generation import (
    EXPORT_FILES,
    STRICT_RUNTIME_VERIFIER_FACTORY,
    construction_metadata,
    export_submission,
    is_retryable_provider_failure,
)
from scripts.run_amq_paper_evaluation import (
    aggregate,
    driver_diagnostic,
    fresh_solver_substitution_metadata,
    merge_d3_backfill_result,
    paper_goe,
)


def _tool(description: str, properties: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        description=description,
        inputSchema={"properties": properties or {}, "required": list((properties or {}).keys())},
    )


def test_generation_uses_production_full_smoke_coverage_gate(tmp_path: Path) -> None:
    verifier = STRICT_RUNTIME_VERIFIER_FACTORY(
        tmp_path,
        SimpleNamespace(tools=[]),
    )

    assert verifier.require_full_smoke_coverage is True


def test_generation_metadata_is_reproducible_and_secret_free() -> None:
    metadata = construction_metadata()

    assert metadata["implementationGitCommit"]
    assert metadata["agentModel"]
    assert metadata["runtimeAcceptance"]["fullSmokeCoverageRequired"] is True
    serialized = json.dumps(metadata).lower()
    assert "api_key" not in serialized
    assert "apikey" not in serialized
    assert "base_url" not in serialized
    assert "baseurl" not in serialized


def test_paper_goe_uses_all_tools_in_information_denominator() -> None:
    tools = [_tool("one two three four five"), _tool("")]

    result = paper_goe(tools, lambda _: {})

    assert result["goe_tool_desc_coverage"] == 0.5
    assert result["goe_tool_desc_informativeness"] == 0.1
    assert result["goe_tool_desc_distinguishability"] == 1.0


def test_paper_goe_counts_range_but_not_pattern_as_constraint() -> None:
    tools = [
        _tool(
            "Returns a result",
            {
                "bounded": {"type": "number", "description": "value", "minimum": 0},
                "regex_only": {"type": "string", "description": "text", "pattern": "x+"},
            },
        )
    ]

    result = paper_goe(tools, lambda _: {})

    assert result["goe_constraint_richness"] == 0.5


def test_export_submission_uses_harness_repo_as_algorithm(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    for name in EXPORT_FILES:
        (artifact / name).write_text("placeholder\n", encoding="utf-8")
    destination = tmp_path / "submissions" / "baseline" / "sample"
    sample = {
        "sample_id": "sample",
        "repo_info": {"url": "https://example.test/repo", "commit_sha": "abc"},
    }

    export_submission(artifact, destination, sample=sample, generation_summary={"status": "ready"})

    dockerfile = (destination / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chown=10001:10001 repo /app/algorithm" in dockerfile
    assert "COPY system-packages.txt /app/system-packages.txt" in dockerfile
    assert "requirements-cpu.txt" in dockerfile
    assert "PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/pip,sharing=locked" in dockerfile
    assert "--no-cache-dir" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "COPY . /app" not in dockerfile
    manifest = json.loads((destination / "generation_manifest.json").read_text(encoding="utf-8"))
    assert manifest["constructionInput"] == "wrap_intent_only"


def test_aggregate_uses_all_expected_samples_as_aqs_denominator() -> None:
    results = [
        {
            "sample_id": "healthy",
            "d1_build_success": True,
            "d1_service_health": True,
            "d2_score": 0.5,
            "d3_pass": True,
            "goe_score": 0.4,
            "gov_score": 0.6,
            "d3_total_calls": 2,
            "d3_successful_calls": 1,
            "d3_tool_call_success_rate": 0.5,
        },
        {
            "sample_id": "failed",
            "d1_build_success": False,
            "d1_service_health": False,
            "d2_score": 0,
            "d3_pass": False,
            "d1_failure_category": "missing_submission",
        },
    ]

    summary = aggregate(results, ["healthy", "failed"])

    assert summary["complete"] is True
    assert summary["aqs"] == 0.4
    assert summary["usabilityHealthyMean"] == 0.5
    assert summary["tcsrPaperMacro"] == 0.5


def test_driver_diagnostic_separates_provider_refusal_from_utility_failure() -> None:
    status, detail = driver_diagnostic(
        {
            "final_answer": "Error code: 403 - prohibited due to provider Terms Of Service",
            "total_calls": 0,
        }
    )

    assert status == "provider_error"
    assert "403" in detail


def test_fresh_d3_solver_substitution_is_explicitly_audited() -> None:
    metadata = fresh_solver_substitution_metadata(
        solver_model="qwen/qwen3.7-max",
        solver_reasoning="disabled",
        substitution_reason="The paper solver is unavailable from the configured provider.",
        skip_d3=False,
    )

    assert metadata["paperSolverModel"] == "openai/gpt-5.4"
    assert metadata["solverConformance"] == "solver_substitution"
    assert metadata["solverReasoning"] == "disabled"
    assert "unavailable" in metadata["solverSubstitutionReason"]
    assert fresh_solver_substitution_metadata(
        solver_model="openai/gpt-5.4",
        solver_reasoning="provider_default",
        substitution_reason=None,
        skip_d3=False,
    ) == {}
    assert fresh_solver_substitution_metadata(
        solver_model="qwen/qwen3.7-max",
        solver_reasoning="disabled",
        substitution_reason="unused",
        skip_d3=True,
    ) == {}


def test_d3_backfill_preserves_d1_d2_and_replaces_only_d3_evidence() -> None:
    original = {
        "sample_id": "sample",
        "d1_build_success": True,
        "d1_service_health": True,
        "d1_image_size_mb": 123.0,
        "d2_score": 0.5,
        "goe_score": 0.4,
        "gov_score": 0.6,
        "d3_pass": False,
        "d3_driver_status": "provider_error",
        "d3_driver_error": "403",
        "d3_total_calls": 0,
        "aqs_score": 0.2,
    }
    rerun = {
        "sample_id": "sample",
        "d1_build_success": True,
        "d1_service_health": True,
        "d1_image_size_mb": 999.0,
        "d2_score": 0.1,
        "goe_score": 0.1,
        "gov_score": 0.1,
        "d3_pass": True,
        "d3_method": "verify_script",
        "d3_total_calls": 2,
        "d3_successful_calls": 2,
        "d3_tool_call_success_rate": 1.0,
    }

    merged = merge_d3_backfill_result(
        original,
        rerun,
        solver_model="qwen/qwen3.7-max",
        solver_reasoning="disabled",
        source_solver_model="openai/gpt-5.4",
        driver_status="completed",
        driver_error="",
        attempted_at="2026-07-17T00:00:00+08:00",
    )

    assert merged["d1_image_size_mb"] == 123.0
    assert merged["d2_score"] == 0.5
    assert merged["goe_score"] == 0.4
    assert merged["gov_score"] == 0.6
    assert merged["d3_pass"] is True
    assert merged["d3_total_calls"] == 2
    assert merged["d3_driver_status"] == "completed"
    assert "d3_driver_error" not in merged
    assert merged["d3_backfill"]["preservedD1D2"] is True
    assert merged["d3_backfill"]["solverReasoning"] == "disabled"
    assert merged["d3_backfill"]["attemptNumber"] == 1
    assert merged["d3_backfill"]["attemptHistory"] == []
    assert merged["aqs_score"] == 0.8


def test_d3_backfill_preserves_failed_runtime_attempt_history() -> None:
    original = {
        "sample_id": "sample",
        "d1_service_health": True,
        "d2_score": 0.5,
        "d3_driver_status": "provider_error",
        "d3_backfill": {
            "attempted": True,
            "attemptedAt": "2026-07-17T00:00:00+08:00",
            "solverModel": "qwen/qwen3.7-max",
            "solverReasoning": "disabled",
            "outcome": "rerun_health_failed",
            "preservedD1D2": True,
            "attemptNumber": 1,
            "attemptHistory": [],
        },
    }
    rerun = {
        "d1_service_health": True,
        "d3_pass": False,
        "d3_total_calls": 1,
        "d3_successful_calls": 0,
    }

    merged = merge_d3_backfill_result(
        original,
        rerun,
        solver_model="qwen/qwen3.7-max",
        solver_reasoning="disabled",
        source_solver_model="openai/gpt-5.4",
        driver_status="completed",
        driver_error="",
        attempted_at="2026-07-17T01:00:00+08:00",
    )

    audit = merged["d3_backfill"]
    assert audit["attemptNumber"] == 2
    assert len(audit["attemptHistory"]) == 1
    assert audit["attemptHistory"][0]["outcome"] == "rerun_health_failed"


def test_generation_resume_retries_credit_failure_but_not_algorithm_failure() -> None:
    assert is_retryable_provider_failure(
        {"status": "failed", "analysisErrors": ["OpenRouter 402: Insufficient credits"]}
    )
    assert not is_retryable_provider_failure(
        {"status": "failed", "analysisErrors": ["no valid packaging plan"]}
    )
    assert not is_retryable_provider_failure(
        {"status": "rejected", "analysisErrors": ["Insufficient credits"]}
    )
