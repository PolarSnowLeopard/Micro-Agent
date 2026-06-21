"""Real-MCP experiment runner for GoldenPath reuse research."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from micro_agent.simulation.artifact_runtime import (
    evaluate_with_verifier,
    run_artifact,
)
from micro_agent.simulation.build_bundle import BuildBundleStore


BASELINES = ("no_reuse", "raw_trace_prompt", "workflow_memory", "golden_path")


def list_experiment_runners() -> list[dict[str, Any]]:
    return [{
        "runnerId": "real_mcp_reuse",
        "available": True,
        "description": "Runs no_reuse/raw_trace_prompt/workflow_memory/golden_path against standardized real MCP services.",
        "baselines": list(BASELINES),
    }]


async def run_experiment(
    *,
    artifact: dict[str, Any],
    tasks: list[dict[str, Any]],
    accepted_trajectory: dict[str, Any] | None = None,
    baselines: list[str] | None = None,
) -> dict[str, Any]:
    selected = [b for b in (baselines or list(BASELINES)) if b in BASELINES]
    results = []
    for task_idx, task in enumerate(tasks, start=1):
        message = task.get("message") or task.get("task") or task.get("input") or ""
        for baseline in selected:
            results.append(await _run_trial(
                artifact=artifact,
                accepted_trajectory=accepted_trajectory or {},
                task_id=str(task.get("taskId") or f"task-{task_idx}"),
                message=message,
                baseline=baseline,
            ))
    return {
        "schemaVersion": "reuse_experiment_result.v1",
        "runnerId": "real_mcp_reuse",
        "baselines": selected,
        "taskCount": len(tasks),
        "trialCount": len(results),
        "metrics": _aggregate(results),
        "trials": results,
    }


async def _run_trial(
    *,
    artifact: dict[str, Any],
    accepted_trajectory: dict[str, Any],
    task_id: str,
    message: str,
    baseline: str,
) -> dict[str, Any]:
    started = time.time()
    trial_message = _baseline_message(baseline, message, accepted_trajectory, artifact)
    run = await run_artifact(
        artifact,
        trial_message,
        prefer_golden_path=(baseline == "golden_path"),
    )
    verdict = await evaluate_with_verifier(artifact, message, run)
    latency = int((time.time() - started) * 1000)
    task_success = verdict.get("verdict") == "passed"
    return {
        "schemaVersion": "reuse_experiment_trial.v1",
        "trialId": f"{task_id}:{baseline}",
        "taskId": task_id,
        "baseline": baseline,
        "taskSuccess": task_success,
        "fastPathSuccess": bool(run.get("fastPathSuccess")),
        "fallbackSuccess": bool(run.get("fallbackUsed") and task_success),
        "overallSuccess": task_success,
        "fallbackUsed": bool(run.get("fallbackUsed")),
        "latencyMs": latency,
        "llmCallCount": None,
        "mcpCallCount": _mcp_call_count(run),
        "tokenUsage": None,
        "plannerIterations": None,
        "verifierPassed": task_success,
        "errorType": _error_type(run, verdict),
        "runResult": run,
        "evalVerifier": verdict,
    }


def _baseline_message(
    baseline: str,
    message: str,
    accepted: dict[str, Any],
    artifact: dict[str, Any],
) -> str:
    if baseline == "no_reuse" or baseline == "golden_path":
        return message
    if baseline == "raw_trace_prompt":
        return (
            "以下是同一元应用历史成功轨迹，请仅作为参考，仍需根据当前任务自主调度 MCP：\n"
            f"{json.dumps(accepted.get('actionSequence') or [], ensure_ascii=False)}\n\n"
            f"当前任务：{message}"
        )
    if baseline == "workflow_memory":
        workflow = [
            {
                "step": s.get("stepId"),
                "serviceId": s.get("serviceId"),
                "toolName": s.get("toolName"),
                "inputMapping": s.get("inputMapping"),
            }
            for gp in artifact.get("goldenPaths") or []
            for s in gp.get("steps") or []
        ]
        return (
            "以下是同一元应用归纳出的工作流记忆，请作为计划参考，仍需自主调度 MCP：\n"
            f"{json.dumps(workflow, ensure_ascii=False)}\n\n"
            f"当前任务：{message}"
        )
    return message


def _aggregate(trials: list[dict[str, Any]]) -> dict[str, Any]:
    by_baseline: dict[str, list[dict[str, Any]]] = {}
    for row in trials:
        by_baseline.setdefault(row["baseline"], []).append(row)
    summary = {}
    for baseline, rows in by_baseline.items():
        n = len(rows) or 1
        summary[baseline] = {
            "trials": len(rows),
            "taskSuccessRate": sum(1 for r in rows if r.get("taskSuccess")) / n,
            "fastPathSuccessRate": sum(1 for r in rows if r.get("fastPathSuccess")) / n,
            "fallbackRate": sum(1 for r in rows if r.get("fallbackUsed")) / n,
            "avgLatencyMs": sum(int(r.get("latencyMs") or 0) for r in rows) / n,
        }
    return summary


def _mcp_call_count(run: dict[str, Any]) -> int | None:
    if run.get("toolCalls"):
        return len(run.get("toolCalls") or [])
    events = run.get("events") or []
    return sum(1 for e in events if e.get("type") == "tool_call")


def _error_type(run: dict[str, Any], verdict: dict[str, Any]) -> str | None:
    if verdict.get("verdict") == "passed":
        return None
    if run.get("fastPathError"):
        text = str(run.get("fastPathError")).lower()
        if "binding" in text or "missing" in text:
            return "binding_error"
        if "tool" in text or "service" in text:
            return "service_error"
    return "verifier_fail"


async def run_experiment_for_build(
    build_id: str,
    tasks: list[dict[str, Any]],
    *,
    baselines: list[str] | None = None,
    store: BuildBundleStore | None = None,
) -> dict[str, Any]:
    store = store or BuildBundleStore()
    artifact = store.load_part(build_id, "artifact")
    if not artifact:
        raise ValueError(f"artifact not found for build {build_id}")
    accepted = store.load_part(build_id, "accepted_trajectory") or {}
    result = await run_experiment(
        artifact=artifact,
        tasks=tasks,
        accepted_trajectory=accepted,
        baselines=baselines,
    )
    path = store.save_experiment_result(build_id, result)
    result["resultPath"] = str(path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run real-MCP meta-app reuse experiments.")
    parser.add_argument("build_id")
    parser.add_argument("--tasks", required=True, help="JSON file with a list of tasks")
    parser.add_argument("--baselines", default=",".join(BASELINES))
    args = parser.parse_args(argv)

    tasks_path = Path(args.tasks)
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        raise SystemExit("--tasks must contain a JSON list")
    baselines = [x.strip() for x in args.baselines.split(",") if x.strip()]
    result = asyncio.run(run_experiment_for_build(args.build_id, tasks, baselines=baselines))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BASELINES",
    "list_experiment_runners",
    "run_experiment",
    "run_experiment_for_build",
]
