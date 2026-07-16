#!/usr/bin/env python3
"""Run AMQ-Bench with the metric protocol stated in the submitted paper.

The released harness remains the execution engine.  This driver applies four
auditable protocol corrections before execution: the GoE equations use all N
tool descriptions, GoV probes at most five tools, Utility stops after eight
turns, and only the deterministic per-sample script decides Utility pass/fail.
Results are checkpointed after every sample and independently aggregated.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import types
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PAPER_CORPUS_SIZE = 269
PAPER_TOOL_PROBE_CAP = 5
PAPER_MAX_UTILITY_TURNS = 8
PAPER_SOLVER_MODEL = "openai/gpt-5.4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strict paper-protocol AMQ-Bench evaluation")
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--benchmark-file", type=Path, required=True)
    parser.add_argument("--submissions-dir", type=Path, required=True)
    parser.add_argument("--results-file", type=Path, required=True)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--repo-cache-root", type=Path, required=True)
    parser.add_argument("--solver-model", default=PAPER_SOLVER_MODEL)
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-d3", action="store_true")
    return parser.parse_args()


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _jaccard_distance(left: set[str], right: set[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return 1.0 - len(left & right) / len(union)


def paper_goe(
    tools: list[Any], parse_docstring_params: Callable[[str], dict[str, str]]
) -> dict[str, Any]:
    """Appendix C GoE, retaining four-decimal harness checkpoints."""
    if not tools:
        return {key: 0 for key in (
            "goe_tool_desc_coverage", "goe_tool_desc_informativeness",
            "goe_tool_desc_distinguishability", "goe_tool_count", "goe_param_count",
            "goe_param_desc_coverage", "goe_param_type_coverage",
            "goe_constraint_richness", "goe_required_clarity",
            "goe_tool_selection_support", "goe_arg_construction_support", "goe_score",
        )}

    descriptions = [tool.description or "" for tool in tools]
    n_tools = len(tools)
    desc_coverage = sum(bool(desc.strip()) for desc in descriptions) / n_tools
    desc_info = min(sum(len(desc.split()) for desc in descriptions) / (25 * n_tools), 1.0)
    if n_tools == 1:
        desc_dist = 1.0
    else:
        token_sets = [set(desc.lower().split()) for desc in descriptions]
        desc_dist = min(
            _jaccard_distance(token_sets[i], token_sets[j])
            for i in range(n_tools)
            for j in range(i + 1, n_tools)
        )

    parameters: list[dict[str, Any]] = []
    range_keys = {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "minItems", "maxItems",
    }
    for tool in tools:
        schema = tool.inputSchema or {}
        properties = schema.get("properties", {})
        required_declared = "required" in schema
        doc_params = parse_docstring_params(tool.description or "")
        for name, info in properties.items():
            description = info.get("description", "") or doc_params.get(name, "")
            constrained = (
                "enum" in info
                or "default" in info
                or "format" in info
                or any(key in info for key in range_keys)
            )
            parameters.append(
                {
                    "description": description,
                    "type": info.get("type", ""),
                    "constrained": constrained,
                    "required_declared": required_declared,
                }
            )

    n_params = len(parameters)
    if n_params:
        param_desc = sum(bool(param["description"].strip()) for param in parameters) / n_params
        param_type = sum(bool(param["type"]) for param in parameters) / n_params
        constraint = sum(param["constrained"] for param in parameters) / n_params
        required_clarity = sum(param["required_declared"] for param in parameters) / n_params
    else:
        param_desc = param_type = constraint = required_clarity = 0.0

    tss = 0.40 * desc_coverage + 0.30 * desc_info + 0.30 * desc_dist
    acs = 0.60 * param_desc + 0.40 * constraint
    goe = 0.40 * tss + 0.60 * acs
    return {
        "goe_tool_desc_coverage": round(desc_coverage, 4),
        "goe_tool_desc_informativeness": round(desc_info, 4),
        "goe_tool_desc_distinguishability": round(desc_dist, 4),
        "goe_tool_count": n_tools,
        "goe_param_count": n_params,
        "goe_param_desc_coverage": round(param_desc, 4),
        "goe_param_type_coverage": round(param_type, 4),
        "goe_constraint_richness": round(constraint, 4),
        "goe_required_clarity": round(required_clarity, 4),
        "goe_tool_selection_support": round(tss, 4),
        "goe_arg_construction_support": round(acs, 4),
        "goe_score": round(goe, 4),
    }


def load_strict_harness(path: Path) -> tuple[types.ModuleType, str]:
    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source = source_bytes.decode("utf-8")
    old = "max_turns = 10"
    if source.count(old) != 1:
        raise RuntimeError(f"expected exactly one {old!r} in released harness")
    source = source.replace(old, f"max_turns = {PAPER_MAX_UTILITY_TURNS}", 1)

    module = types.ModuleType("amq_bench_strict_runtime")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(source, str(path), "exec"), module.__dict__)

    original_gov = module.MetricCalculator.evaluate_gov

    async def strict_gov(session: Any, tools: list[Any]) -> dict[str, Any]:
        selected = tools[:PAPER_TOOL_PROBE_CAP]
        result = await original_gov(session, selected)
        result["gov_total_tools"] = len(tools)
        result["gov_probe_cap"] = PAPER_TOOL_PROBE_CAP
        result["gov_selected_tools"] = [tool.name for tool in selected]
        return result

    async def deterministic_d3(
        agent_result: dict[str, Any],
        criteria: dict[str, Any],
        task_intent: str = "",
        ground_truth: dict[str, Any] | None = None,
        verify_model: str = "unused",
    ) -> dict[str, Any]:
        del task_intent, verify_model
        passed = False
        method = "verify_script"
        script = criteria.get("verify_script")
        if not script:
            method = "missing_verify_script"
        else:
            try:
                exec_globals = {
                    "final_response": agent_result.get("final_answer", ""),
                    "agent_result": agent_result,
                    "re": __import__("re"),
                    "json": __import__("json"),
                    "math": __import__("math"),
                }
                exec(script, exec_globals)
                passed = True
            except Exception:
                passed = False

        gt = ground_truth or {}
        first_name = agent_result.get("first_tool_name")
        first_correct = module.MetricCalculator._first_tool_matches(
            first_name, gt.get("expected_capabilities", [])
        )
        return {
            "pass": passed,
            "method": method,
            "llm_verdict": "",
            "turns": agent_result.get("turns", 0),
            "errors": agent_result.get("errors", 0),
            "total_calls": agent_result.get("total_calls", 0),
            "successful_calls": agent_result.get("successful_calls", 0),
            "tool_call_success_rate": agent_result.get("tool_call_success_rate", 0.0),
            "first_tool_correct": first_correct,
            "first_tool_name": first_name,
            "retry_count": agent_result.get("retry_count", 0),
        }

    module.MetricCalculator.evaluate_goe = staticmethod(
        lambda tools: paper_goe(tools, module.MetricCalculator._parse_docstring_params)
    )
    module.MetricCalculator.evaluate_gov = staticmethod(strict_gov)
    module.MetricCalculator.evaluate_d3 = staticmethod(deterministic_d3)
    return module, source_sha256


def aggregate(results: list[dict[str, Any]], expected_ids: list[str]) -> dict[str, Any]:
    by_id = {result["sample_id"]: result for result in results}
    duplicates = len(results) - len(by_id)
    missing = [sample_id for sample_id in expected_ids if sample_id not in by_id]
    ordered = [by_id[sample_id] for sample_id in expected_ids if sample_id in by_id]
    healthy = [result for result in ordered if result.get("d1_service_health")]
    active_calls = [result for result in ordered if result.get("d3_total_calls", 0) > 0]
    total_calls = sum(result.get("d3_total_calls", 0) for result in ordered)
    successful_calls = sum(result.get("d3_successful_calls", 0) for result in ordered)

    for result in ordered:
        d1 = 1.0 if result.get("d1_service_health") else 0.0
        d2 = float(result.get("d2_score", 0.0))
        d3 = 1.0 if result.get("d3_pass") else 0.0
        result["aqs_score"] = round(d1 * (0.4 * d2 + 0.6 * d3), 4)

    denominator = len(expected_ids)
    mean = lambda values: sum(values) / len(values) if values else None
    failure_counts = Counter(
        result.get("d1_failure_category", "healthy") if not result.get("d1_service_health") else "healthy"
        for result in ordered
    )
    return {
        "complete": not missing and not duplicates and len(ordered) == denominator,
        "expectedSamples": denominator,
        "completedSamples": len(ordered),
        "missingSampleIds": missing,
        "duplicateResultCount": duplicates,
        "build": sum(bool(result.get("d1_build_success")) for result in ordered),
        "health": len(healthy),
        "goeHealthyMean": mean([float(result.get("goe_score", 0.0)) for result in healthy]),
        "govHealthyMean": mean([float(result.get("gov_score", 0.0)) for result in healthy]),
        "usabilityHealthyMean": mean([float(result.get("d2_score", 0.0)) for result in healthy]),
        "utility": sum(bool(result.get("d3_pass")) for result in ordered),
        "aqs": sum(float(result.get("aqs_score", 0.0)) for result in ordered) / denominator,
        "tcsrPaperMacro": mean(
            [float(result.get("d3_tool_call_success_rate", 0.0)) for result in active_calls]
        ),
        "tcsrMicro": successful_calls / total_calls if total_calls else None,
        "activeCallSamples": len(active_calls),
        "successfulToolCalls": successful_calls,
        "totalToolCalls": total_calls,
        "failureCounts": dict(sorted(failure_counts.items())),
    }


async def main() -> int:
    args = parse_args()
    if args.solver_model != PAPER_SOLVER_MODEL and not args.skip_d3:
        raise SystemExit(f"paper protocol requires --solver-model {PAPER_SOLVER_MODEL}")

    harness, source_sha256 = load_strict_harness(args.harness.resolve())
    benchmark_file = args.benchmark_file.resolve()
    samples = [
        json.loads(line)
        for line in benchmark_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(samples) != PAPER_CORPUS_SIZE and not args.sample:
        raise SystemExit(f"paper protocol requires {PAPER_CORPUS_SIZE} samples, got {len(samples)}")
    all_ids = [sample["sample_id"] for sample in samples]
    if len(set(all_ids)) != len(all_ids):
        raise SystemExit("benchmark contains duplicate sample_id values")
    selected = set(args.sample)
    tasks = [sample for sample in samples if not selected or sample["sample_id"] in selected]
    expected_ids = [sample["sample_id"] for sample in tasks]
    if selected and len(tasks) != len(selected):
        raise SystemExit(f"unknown sample ids: {sorted(selected - set(expected_ids))}")
    if any(not sample.get("evaluation_criteria", {}).get("verify_script") for sample in tasks):
        raise SystemExit("every evaluated sample must provide a deterministic verify_script")

    harness.SUBMISSIONS_DIR = str(args.submissions_dir.resolve())
    harness.REPO_CACHE_ROOT = str(args.repo_cache_root.resolve())
    harness.REPO_SEARCH_DIRS = [harness.REPO_CACHE_ROOT]
    harness.RESULTS_DIR = str(args.results_file.resolve().parent)
    harness.LOGS_DIR = str(args.results_file.resolve().parent / "logs")
    os.makedirs(harness.LOGS_DIR, exist_ok=True)

    protocol = {
        "schemaVersion": "amq-bench-paper-protocol/v1",
        "asOf": datetime.now().astimezone().isoformat(),
        "baselineId": args.baseline_id,
        "benchmarkFile": str(benchmark_file),
        "benchmarkSha256": hashlib.sha256(benchmark_file.read_bytes()).hexdigest(),
        "releasedHarness": str(args.harness.resolve()),
        "releasedHarnessSha256": source_sha256,
        "sampleCount": len(tasks),
        "solverModel": args.solver_model,
        "solverTemperature": 0.0,
        "solverSeed": 42,
        "maxUtilityTurns": PAPER_MAX_UTILITY_TURNS,
        "utilityOracle": "deterministic_verify_script_only",
        "goVToolProbeCap": PAPER_TOOL_PROBE_CAP,
        "aqsFormula": "d1 * (0.4 * d2 + 0.6 * d3)",
        "methodAqsDenominator": len(expected_ids),
    }

    results_file = args.results_file.resolve()
    completed: dict[str, dict[str, Any]] = {}
    if args.resume and results_file.is_file():
        prior = json.loads(results_file.read_text(encoding="utf-8"))
        completed = {result["sample_id"]: result for result in prior.get("results", [])}

    runner = harness.BenchmarkRunner(
        solver_model=args.solver_model,
        baseline_id=args.baseline_id,
        skip_d3=args.skip_d3,
        verify_model="deterministic-only",
    )
    log_path = results_file.parent / "logs" / f"{results_file.stem}.log"
    file_handler = harness.add_file_handler(harness.logger, str(log_path))
    try:
        for index, task in enumerate(tasks, start=1):
            sample_id = task["sample_id"]
            if sample_id in completed:
                harness.logger.info("[%d/%d] resume skip %s", index, len(tasks), sample_id)
                continue
            harness.logger.info("[%d/%d] strict paper evaluation", index, len(tasks))
            result = await runner.run_task(task)
            d1 = 1.0 if result.get("d1_service_health") else 0.0
            d2 = float(result.get("d2_score", 0.0))
            d3 = 1.0 if result.get("d3_pass") else 0.0
            result["aqs_score"] = round(d1 * (0.4 * d2 + 0.6 * d3), 4)
            completed[sample_id] = result
            ordered = [completed[sid] for sid in expected_ids if sid in completed]
            payload = {
                "protocol": protocol,
                "summary": aggregate(ordered, expected_ids),
                "results": ordered,
            }
            _write_json_atomic(results_file, payload)
    finally:
        harness.logger.removeHandler(file_handler)
        file_handler.close()

    ordered = [completed[sid] for sid in expected_ids if sid in completed]
    final_payload = {
        "protocol": protocol,
        "summary": aggregate(ordered, expected_ids),
        "results": ordered,
    }
    _write_json_atomic(results_file, final_payload)
    print(json.dumps(final_payload["summary"], ensure_ascii=False, indent=2), flush=True)
    return 0 if final_payload["summary"]["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
