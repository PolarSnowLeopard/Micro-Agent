#!/usr/bin/env python3
"""Run one reproducible Agent planning/packaging evaluation outside the web UI."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.models import PackagingPlan
from micro_agent.packaging.runtime_verifier import ContainerRuntimeVerifier
from micro_agent.packaging.workflow import (
    AgenticAnalysisWorkflow,
    AgenticPackagingWorkflow,
    planning_candidate_symbols,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path, help="Extracted algorithm repository root")
    parser.add_argument("--output", type=Path, required=True, help="Evaluation output directory")
    parser.add_argument("--plan-only", action="store_true", help="Stop after semantic planning")
    parser.add_argument("--plan-file", type=Path, help="Reuse a previously reviewed plan and skip the planning Agent")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    project = args.project.resolve()
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    ir = RepositoryAnalyzer().analyze(project)
    (output / "repository_ir.json").write_text(ir.to_json(indent=2) + "\n", encoding="utf-8")
    event_log = output / "events.jsonl"
    analysis_events = []
    if args.plan_file:
        plan = PackagingPlan.validate(
            json.loads(args.plan_file.read_text(encoding="utf-8")),
            known_symbols=ir.known_symbols,
            known_files={file.path for file in ir.files},
            symbol_required_parameters={
                symbol.qualifiedName: symbol.requiredParameters for symbol in ir.symbols
            },
            symbol_calls={symbol.qualifiedName: symbol.calls for symbol in ir.symbols},
            symbol_is_generator={symbol.qualifiedName: symbol.isGenerator for symbol in ir.symbols},
            candidate_symbols=planning_candidate_symbols(ir),
        )
        analysis_seconds = 0.0
        (output / "function.json").write_text(
            json.dumps(plan.to_frontend_graph(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        graph_path = output / "function.json"
        analysis = AgenticAnalysisWorkflow(project_dir=project, ir=ir, graph_path=graph_path)
        analysis_started = time.perf_counter()
        async for event in analysis.run("独立算法封装评测"):
            analysis_events.append(event)
            _append_event(event_log, "analysis", event)
            _print_event("analysis", event)
        analysis_seconds = time.perf_counter() - analysis_started
        plan = analysis.plan_store.plan
    summary = {
        "repositoryFingerprint": ir.fingerprint,
        "filesScanned": len(ir.files),
        "symbolsScanned": len(ir.symbols),
        "parseErrorCount": len(ir.parseErrors),
        "analysisSeconds": round(analysis_seconds, 3),
        "decision": plan.decision if plan else "failed",
        "serviceCount": len(plan.data.get("services", [])) if plan else 0,
        "toolCount": len(plan.tools) if plan else 0,
        "tools": plan.tool_names if plan else [],
        "analysisErrors": [event.data.get("error", "") for event in analysis_events if event.type == "error"],
        "artifactReady": False,
    }
    if plan:
        (output / "packaging_plan.json").write_text(plan.to_json() + "\n", encoding="utf-8")

    if not plan or plan.decision != "package":
        _write_summary(output, summary)
        return 2
    if args.plan_only:
        _write_summary(output, summary)
        return 0

    artifact = output / "artifact"
    packaging = AgenticPackagingWorkflow(
        project_dir=project,
        ir=ir,
        artifact_dir=artifact,
        plan=plan,
        runtime_verifier_factory=ContainerRuntimeVerifier,
    )
    packaging_started = time.perf_counter()
    packaging_events = []
    async for event in packaging.run("独立算法封装评测"):
        packaging_events.append(event)
        _append_event(event_log, "packaging", event)
        _print_event("packaging", event)
    packaging_seconds = time.perf_counter() - packaging_started
    summary.update(
        {
            "packagingSeconds": round(packaging_seconds, 3),
            "packagingErrors": [
                event.data.get("error", "") for event in packaging_events if event.type == "error"
            ],
            "artifactReady": (artifact / ".ioeb-ready").is_file(),
        }
    )
    if (artifact / ".ioeb-ready").is_file():
        summary["readyMarker"] = json.loads((artifact / ".ioeb-ready").read_text(encoding="utf-8"))
    if (artifact / "verification_report.json").is_file():
        summary["verification"] = json.loads(
            (artifact / "verification_report.json").read_text(encoding="utf-8")
        )
    if (artifact / "runtime_verification_report.json").is_file():
        summary["runtimeVerification"] = json.loads(
            (artifact / "runtime_verification_report.json").read_text(encoding="utf-8")
        )
    _write_summary(output, summary)
    return 0 if summary["artifactReady"] else 3


def _append_event(path: Path, phase: str, event) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"phase": phase, **event.to_dict()}, ensure_ascii=False, default=str) + "\n"
        )


def _print_event(phase: str, event) -> None:
    detail = event.data.get("tool") or event.data.get("error") or event.data.get("result")
    if not detail:
        detail = event.data.get("thought", "")
    print(f"[{phase}] step={event.step} type={event.type} {str(detail)[:300]}", flush=True)


def _write_summary(output: Path, summary: dict) -> None:
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
