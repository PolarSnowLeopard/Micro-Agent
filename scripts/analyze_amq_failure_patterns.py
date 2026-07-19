#!/usr/bin/env python3
"""Aggregate reusable AMQ adaptation and generation failure patterns.

The report deliberately avoids benchmark task/ground-truth fields. It reads
only derived repository contracts, validation reports, and generation-run
telemetry so repeated experiments do not get mistaken for independent samples.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from micro_agent.packaging.template_adapter import (  # noqa: E402
    _literal_dispatch_cases,
    _runtime_requirement_errors,
    validate_algorithm_template,
)


GENERATION_FAILURE_PATTERNS: dict[str, tuple[str, ...]] = {
    "smoke_fixture_grounding": (
        "smoke_evidence_reference",
        "smoke_fixture_grounding",
        "smoketest.input",
        "smoke_test",
        "smoke_coverage",
    ),
    "source_api_grounding": (
        "source_import",
        "runtime_api_compatibility",
        "未知符号",
        "不存在的成员",
        "cannot import name",
    ),
    "source_execution_fidelity": (
        "未调用规划中的任何源码能力",
        "禁止用另一个库重写算法",
        "structured failure",
        "普通字符串返回",
        "except 中返回普通结果",
    ),
    "schema_runtime_mismatch": (
        "output schema mismatch",
        "outputschema",
        "validation error",
        "参数与规划不一致",
        "inputschema 参数不足",
        "dispatch_coverage",
        "interface_quality",
    ),
    "dependency_or_build": (
        "contract_build",
        "容器构建",
        "no matching distribution",
        "modulenotfounderror",
        "依赖",
    ),
    "agent_submission_protocol": (
        "未能提交有效封装规划",
        "未调用 save_packaging_plan",
        "不是严格 json",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate cross-sample AMQ failure patterns"
    )
    parser.add_argument(
        "--adaptation-root",
        type=Path,
        action="append",
        default=[],
        help="Root containing adapted_repos/<sample_id>; repeatable.",
    )
    parser.add_argument(
        "--generation-root",
        type=Path,
        action="append",
        default=[],
        help="Root recursively containing generation_summary.json; repeatable.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def classify_generation_failure(text: str) -> list[str]:
    lowered = text.lower()
    labels = [
        label
        for label, markers in GENERATION_FAILURE_PATTERNS.items()
        if any(marker.lower() in lowered for marker in markers)
    ]
    return labels or ["unclassified"]


def classify_adaptation_errors(errors: list[str]) -> list[str]:
    text = "\n".join(errors).lower()
    labels: list[str] = []
    patterns = {
        "executable_contract_missing": (
            "test_template_contract.py",
            "模板契约未执行",
        ),
        "source_api_grounding": (
            "不存在的成员",
            "未调用任何从原仓库导入",
        ),
        "unsafe_dynamic_execution": ("动态执行用户文本",),
        "interface_overbroad": (
            "显式参数过多",
            "operation 过多",
            "fixture 过多",
            "*args 或 **kwargs",
        ),
        "module_runtime_state": ("模块级调用",),
        "schema_or_doc_contract": (
            "类型注解",
            "google 风格",
            "docstring",
            "json 字面量",
            "分支",
        ),
        "dependency_manifest": (
            "requirements.txt",
            "pep 508",
            "url/vcs",
        ),
    }
    for label, markers in patterns.items():
        if any(marker.lower() in text for marker in markers):
            labels.append(label)
    return labels or (["passed"] if not errors else ["unclassified"])


def analyze_adaptation_repo(sample_id: str, repo: Path) -> dict[str, Any]:
    report = validate_algorithm_template(repo, require_contract_test=True)
    parameters: list[str] = []
    dispatch_counts: dict[str, int] = {}
    main_path = repo / "main.py"
    if main_path.is_file():
        try:
            tree = ast.parse(
                main_path.read_text(encoding="utf-8", errors="replace")
            )
        except SyntaxError:
            tree = None
        if tree is not None:
            functions = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "main_process"
            ]
            if len(functions) == 1:
                function = functions[0]
                parameters = [
                    item.arg
                    for item in (
                        *function.args.posonlyargs,
                        *function.args.args,
                        *function.args.kwonlyargs,
                    )
                ]
                dispatch_counts = {
                    name: len(values)
                    for name, values in _literal_dispatch_cases(function).items()
                }
    path_like = [
        name
        for name in parameters
        if any(
            marker in name.lower()
            for marker in ("dir", "file", "folder", "path")
        )
    ]
    metadata: dict[str, Any] = {}
    metadata_path = repo / "template_adaptation.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(
                metadata_path.read_text(encoding="utf-8", errors="replace")
            )
        except json.JSONDecodeError:
            metadata = {}
    runtime = metadata.get("contractRuntime")
    runtime_passed = bool(
        isinstance(runtime, dict)
        and runtime.get("passed")
        and runtime.get("checks", {}).get("functionalVerified")
    )
    requirement_errors = _runtime_requirement_errors(
        repo / "requirements.txt"
    )
    errors = [*report.errors, *requirement_errors]
    return {
        "sampleId": sample_id,
        "templatePassed": report.passed,
        "contractRuntimePassed": runtime_passed,
        "failurePatterns": classify_adaptation_errors(errors),
        "parameterCount": len(parameters),
        "pathLikeParameters": path_like,
        "dispatchCounts": dispatch_counts,
        "contractFixtureCount": len(
            report.checks.get("contractFixtures", [])
        ),
        "errors": errors,
    }


def _adaptation_cross_section(roots: list[Path]) -> dict[str, Any]:
    repositories: dict[str, Path] = {}
    for root in roots:
        adapted = root.resolve() / "adapted_repos"
        if not adapted.is_dir():
            continue
        for repo in sorted(adapted.iterdir()):
            if repo.is_dir() and not repo.name.startswith("."):
                repositories[repo.name] = repo
    rows = [
        analyze_adaptation_repo(sample_id, repo)
        for sample_id, repo in sorted(repositories.items())
    ]
    patterns = Counter(
        pattern for row in rows for pattern in row["failurePatterns"]
    )
    return {
        "sampleCount": len(rows),
        "templatePassedCount": sum(row["templatePassed"] for row in rows),
        "contractRuntimePassedCount": sum(
            row["contractRuntimePassed"] for row in rows
        ),
        "pathInterfaceSampleCount": sum(
            bool(row["pathLikeParameters"]) for row in rows
        ),
        "parameterCountDistribution": dict(
            sorted(Counter(row["parameterCount"] for row in rows).items())
        ),
        "failurePatternCounts": dict(patterns.most_common()),
        "samples": rows,
    }


def _generation_history(roots: list[Path]) -> dict[str, Any]:
    paths: set[Path] = set()
    for root in roots:
        paths.update(root.resolve().rglob("generation_summary.json"))
    status_counts: Counter[str] = Counter()
    sample_frequency: Counter[str] = Counter()
    pattern_events: Counter[str] = Counter()
    affected_samples: defaultdict[str, set[str]] = defaultdict(set)
    result_count = 0
    for path in sorted(paths):
        try:
            summary = json.loads(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except json.JSONDecodeError:
            continue
        for result in summary.get("results", []):
            if not isinstance(result, dict):
                continue
            result_count += 1
            sample_id = str(result.get("sampleId", "<unknown>"))
            status = str(result.get("status", "<unknown>"))
            status_counts[status] += 1
            sample_frequency[sample_id] += 1
            if status == "ready":
                continue
            text = "\n".join(
                [
                    *map(str, result.get("analysisErrors", [])),
                    *map(str, result.get("packagingErrors", [])),
                    *map(
                        str,
                        result.get("runtimeVerification", {}).get(
                            "errors",
                            [],
                        ),
                    ),
                    str(result.get("error", "")),
                ]
            )
            for label in classify_generation_failure(text):
                pattern_events[label] += 1
                affected_samples[label].add(sample_id)
    return {
        "runCount": len(paths),
        "resultCount": result_count,
        "statusCounts": dict(status_counts),
        "sampleFrequency": dict(sample_frequency.most_common()),
        "failurePatternEventCounts": dict(pattern_events.most_common()),
        "failurePatternAffectedSamples": {
            label: sorted(samples)
            for label, samples in sorted(affected_samples.items())
        },
        "interpretationNote": (
            "Event counts describe iterative diagnostics, not mini30 "
            "prevalence; sampleFrequency exposes repeated-sample bias."
        ),
    }


def build_report(
    adaptation_roots: list[Path],
    generation_roots: list[Path],
) -> dict[str, Any]:
    return {
        "schemaVersion": "ioeb.amq-failure-patterns/v1",
        "generatedAt": datetime.now().astimezone().isoformat(),
        "dataPolicy": (
            "Derived contracts and run telemetry only; benchmark task and "
            "ground-truth fields are not read."
        ),
        "adaptationCrossSection": _adaptation_cross_section(
            adaptation_roots
        ),
        "generationHistory": _generation_history(generation_roots),
    }


def main() -> int:
    args = parse_args()
    report = build_report(args.adaptation_root, args.generation_root)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
