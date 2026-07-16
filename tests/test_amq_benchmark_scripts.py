from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.run_amq_agentic_generation import EXPORT_FILES, export_submission
from scripts.run_amq_paper_evaluation import aggregate, driver_diagnostic, paper_goe


def _tool(description: str, properties: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        description=description,
        inputSchema={"properties": properties or {}, "required": list((properties or {}).keys())},
    )


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
    assert "COPY repo /app/algorithm" in dockerfile
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
