#!/usr/bin/env python3
"""Generate resumable AMQ-Bench submissions with the production Agent workflow.

Only ``wrap_intent`` is exposed to the planner/builder.  Ground-truth fields and
the downstream verification task stay outside the construction process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.workflow import AgenticAnalysisWorkflow, AgenticPackagingWorkflow


EXPORT_FILES = (
    "server.py",
    "adapters.py",
    "algorithm_loader.py",
    "runtime_guardrails.py",
    "requirements.txt",
    "packaging_plan.json",
    "ioeb-service.json",
)

RETRYABLE_PROVIDER_MARKERS = (
    "insufficient credits",
    "ratelimiterror",
    "rate limit",
    "service unavailable",
    "connectionerror",
    "connection error",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate AMQ-Bench submissions with IOEB Agent packaging")
    parser.add_argument("--benchmark-file", type=Path, required=True)
    parser.add_argument("--submissions-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--repo-cache-root", type=Path, action="append", default=[])
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def load_samples(path: Path, selected: set[str], limit: int | None) -> list[dict[str, Any]]:
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if selected:
        samples = [sample for sample in samples if sample["sample_id"] in selected]
    if limit is not None:
        samples = samples[:limit]
    return samples


def _valid_repo(path: Path, expected_commit: str) -> bool:
    if not (path / ".git" / "HEAD").is_file():
        return False
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0 and result.stdout.strip() == expected_commit


def _cached_repo(cache_roots: list[Path], sample_id: str, commit: str) -> Path | None:
    suffixes = (Path(sample_id), Path(sample_id) / "source", Path(sample_id) / "cursor_work" / "repo")
    for root in cache_roots:
        for suffix in suffixes:
            candidate = root / suffix
            if _valid_repo(candidate, commit):
                return candidate
    return None


def prepare_source(
    sample: dict[str, Any], destination: Path, cache_roots: list[Path]
) -> tuple[str, float]:
    started = time.perf_counter()
    info = sample["repo_info"]
    commit = info["commit_sha"]
    cached = _cached_repo(cache_roots, sample["sample_id"], commit)
    source = "clone"
    if cached is not None:
        source = str(cached)
        for command in (["cp", "-al", str(cached), str(destination)], ["cp", "-a", str(cached), str(destination)]):
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if result.returncode == 0 and _valid_repo(destination, commit):
                return source, time.perf_counter() - started
            shutil.rmtree(destination, ignore_errors=True)

    subprocess.run(
        ["git", "clone", "--quiet", info["url"], str(destination)], check=True, timeout=600
    )
    subprocess.run(
        ["git", "checkout", "--quiet", commit], cwd=destination, check=True, timeout=60
    )
    return source, time.perf_counter() - started


def _submission_dockerfile() -> str:
    return (
        "FROM python:3.11-slim\n"
        "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
        "WORKDIR /app\n"
        "COPY requirements.txt /app/requirements.txt\n"
        "RUN pip install --no-cache-dir --index-url \"${PIP_INDEX_URL}\" "
        "--timeout 120 --retries 5 -r /app/requirements.txt\n"
        "COPY repo /app/algorithm\n"
        "COPY server.py adapters.py algorithm_loader.py runtime_guardrails.py /app/\n"
        "RUN touch /app/algorithm/__init__.py\n"
        "EXPOSE 8000\n"
        "CMD [\"python\", \"server.py\"]\n"
    )


def export_submission(
    artifact: Path,
    destination: Path,
    *,
    sample: dict[str, Any],
    generation_summary: dict[str, Any],
) -> None:
    missing = [name for name in EXPORT_FILES if not (artifact / name).is_file()]
    if missing:
        raise RuntimeError(f"artifact missing export files: {', '.join(missing)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for name in EXPORT_FILES:
            shutil.copy2(artifact / name, temp / name)
        (temp / "Dockerfile").write_text(_submission_dockerfile(), encoding="utf-8")
        (temp / ".dockerignore").write_text(
            "repo/.git\nrepo/.github\nrepo/**/__pycache__\nrepo/.venv\nrepo/venv\n",
            encoding="utf-8",
        )
        manifest = {
            "schemaVersion": "ioeb.amq-generation/v1",
            "sampleId": sample["sample_id"],
            "repository": sample["repo_info"],
            "constructionInput": "wrap_intent_only",
            "summary": generation_summary,
        }
        (temp / "generation_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            shutil.rmtree(destination)
        temp.rename(destination)
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def _append_event(path: Path, phase: str, event: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"phase": phase, **event.to_dict()}, ensure_ascii=False, default=str) + "\n")


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def is_retryable_provider_failure(summary: dict[str, Any]) -> bool:
    if summary.get("status") != "failed":
        return False
    serialized = json.dumps(summary, ensure_ascii=False).lower()
    return any(marker in serialized for marker in RETRYABLE_PROVIDER_MARKERS)


async def generate_one(
    sample: dict[str, Any],
    *,
    submissions_root: Path,
    run_dir: Path,
    cache_roots: list[Path],
    resume: bool,
) -> dict[str, Any]:
    sample_id = sample["sample_id"]
    output = run_dir / "generation" / sample_id
    submission = submissions_root / sample_id
    summary_path = output / "summary.json"
    if resume and summary_path.is_file():
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if is_retryable_provider_failure(previous):
            print(f"[{sample_id}] resume retry: provider infrastructure failure", flush=True)
        elif previous.get("status") in {"ready", "rejected", "failed"}:
            print(f"[{sample_id}] resume: {previous['status']}", flush=True)
            return previous

    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    event_log = output / "events.jsonl"
    started = time.perf_counter()
    result: dict[str, Any] = {
        "sampleId": sample_id,
        "status": "failed",
        "constructionInput": "wrap_intent_only",
        "artifactReady": False,
    }
    sources_root = run_dir / "sources"
    sources_root.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix=f"{sample_id}-", dir=sources_root) as temp_dir:
            project = Path(temp_dir) / "repo"
            cache_source, clone_seconds = await asyncio.to_thread(
                prepare_source, sample, project, cache_roots
            )
            result.update({"repositorySource": cache_source, "repositorySeconds": round(clone_seconds, 3)})

            ir = await asyncio.to_thread(RepositoryAnalyzer().analyze, project)
            (output / "repository_ir.json").write_text(ir.to_json(indent=2) + "\n", encoding="utf-8")
            result.update(
                {
                    "repositoryFingerprint": ir.fingerprint,
                    "filesScanned": len(ir.files),
                    "symbolsScanned": len(ir.symbols),
                    "parseErrorCount": len(ir.parseErrors),
                    "repositoryTruncated": ir.truncated,
                }
            )

            request = sample["wrap_intent"]
            analysis = AgenticAnalysisWorkflow(
                project_dir=project, ir=ir, graph_path=output / "function.json"
            )
            analysis_started = time.perf_counter()
            analysis_errors: list[str] = []
            async for event in analysis.run(request):
                _append_event(event_log, "analysis", event)
                if event.type == "error":
                    analysis_errors.append(str(event.data.get("error", "")))
            result["analysisSeconds"] = round(time.perf_counter() - analysis_started, 3)
            result["analysisErrors"] = analysis_errors
            plan = analysis.plan_store.plan
            if plan is None:
                result["decision"] = "failed"
                result["failureStage"] = "analysis"
                return result

            (output / "packaging_plan.json").write_text(plan.to_json() + "\n", encoding="utf-8")
            result.update(
                {
                    "decision": plan.decision,
                    "serviceCount": len(plan.data.get("services", [])),
                    "toolCount": len(plan.tools),
                    "tools": plan.tool_names,
                }
            )
            if plan.decision != "package":
                result["status"] = "rejected"
                result["rejectionReasons"] = plan.data.get("rejectionReasons", [])
                shutil.rmtree(submission, ignore_errors=True)
                return result

            artifact = output / "artifact"
            workflow = AgenticPackagingWorkflow(
                project_dir=project, ir=ir, artifact_dir=artifact, plan=plan
            )
            packaging_started = time.perf_counter()
            packaging_errors: list[str] = []
            async for event in workflow.run(request):
                _append_event(event_log, "packaging", event)
                if event.type == "error":
                    packaging_errors.append(str(event.data.get("error", "")))
            result["packagingSeconds"] = round(time.perf_counter() - packaging_started, 3)
            result["packagingErrors"] = packaging_errors
            result["artifactReady"] = (artifact / ".ioeb-ready").is_file()
            if not result["artifactReady"]:
                result["failureStage"] = "packaging"
                shutil.rmtree(submission, ignore_errors=True)
                return result

            if (artifact / "verification_report.json").is_file():
                result["verification"] = json.loads(
                    (artifact / "verification_report.json").read_text(encoding="utf-8")
                )
            export_submission(artifact, submission, sample=sample, generation_summary=result)
            shutil.rmtree(artifact / "algorithm", ignore_errors=True)
            result["status"] = "ready"
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["failureStage"] = result.get("failureStage", "system")
        shutil.rmtree(submission, ignore_errors=True)
        return result
    finally:
        result["totalSeconds"] = round(time.perf_counter() - started, 3)
        _write_json_atomic(summary_path, result)
        print(
            f"[{sample_id}] {result['status']} tools={result.get('toolCount', 0)} "
            f"seconds={result['totalSeconds']}",
            flush=True,
        )


async def main() -> int:
    args = parse_args()
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    samples = load_samples(args.benchmark_file.resolve(), set(args.sample), args.limit)
    run_dir = args.run_dir.resolve()
    submissions_root = args.submissions_dir.resolve() / args.baseline_id
    run_dir.mkdir(parents=True, exist_ok=True)
    submissions_root.mkdir(parents=True, exist_ok=True)
    cache_roots = [path.resolve() for path in args.repo_cache_root]
    if not cache_roots:
        cache_roots = [Path(os.getenv("REPO_CACHE_ROOT", ".repo_cache")).resolve()]

    protocol = {
        "schemaVersion": "ioeb.amq-generation-run/v1",
        "baselineId": args.baseline_id,
        "benchmarkFile": str(args.benchmark_file.resolve()),
        "sampleCount": len(samples),
        "constructionInput": "wrap_intent_only",
        "concurrency": args.concurrency,
        "repoCacheRoots": [str(path) for path in cache_roots],
    }
    _write_json_atomic(run_dir / "generation_protocol.json", protocol)

    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(sample: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await generate_one(
                sample,
                submissions_root=submissions_root,
                run_dir=run_dir,
                cache_roots=cache_roots,
                resume=args.resume,
            )

    results = await asyncio.gather(*(bounded(sample) for sample in samples))
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    _write_json_atomic(
        run_dir / "generation_summary.json",
        {**protocol, "statusCounts": counts, "results": results},
    )
    print(json.dumps(counts, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
