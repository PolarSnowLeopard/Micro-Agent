#!/usr/bin/env python3
"""Create a non-destructive, template-adapted AMQ-Bench subset.

The adapter sees only source code and ``wrap_intent``. Benchmark tasks, ground
truth, and deterministic verification criteria are copied into the derived
JSONL only after adaptation, and are never provided to the construction Agent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.template_adapter import (
    build_template_adapter_agent,
    template_adapter_prompt,
    validate_algorithm_template,
)
from scripts.run_amq_agentic_generation import prepare_source


EXPECTED_MINI30_SIZE = 30
PROTECTED_FIELDS = (
    "sample_id",
    "wrap_intent",
    "task",
    "ground_truth",
    "evaluation_criteria",
    "difficulty",
    "tags",
    "required_env",
    "_label_meta",
)
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adapt official AMQ mini30 to the IOEB ZIP template")
    parser.add_argument("--benchmark-file", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-cache-root", type=Path, action="append", default=[])
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_mini30(path: Path) -> list[dict[str, Any]]:
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [sample.get("sample_id") for sample in samples]
    if len(samples) != EXPECTED_MINI30_SIZE:
        raise ValueError(f"mini30 must contain exactly {EXPECTED_MINI30_SIZE} rows, got {len(samples)}")
    if len(set(ids)) != len(ids) or any(not sample_id for sample_id in ids):
        raise ValueError("mini30 contains missing or duplicate sample_id values")
    for sample in samples:
        if not sample.get("wrap_intent") or not sample.get("repo_info", {}).get("url"):
            raise ValueError(f"sample is missing wrap_intent/repo_info: {sample.get('sample_id')}")
    return samples


def ensure_output_outside_source_repo(benchmark_file: Path, output_root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(benchmark_file.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return
    source_root = Path(result.stdout.strip()).resolve()
    try:
        output_root.resolve().relative_to(source_root)
    except ValueError:
        return
    raise ValueError(f"output root must be outside the original AMQ-Bench repository: {source_root}")


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_l0(sample: dict[str, Any]) -> bool:
    candidates = [
        sample.get("complexity"),
        sample.get("complexity_level"),
        sample.get("metadata", {}).get("complexity"),
        sample.get("metadata", {}).get("complexity_level"),
        sample.get("repo_info", {}).get("category"),
    ]
    return any(str(value).strip().upper().startswith("L0") for value in candidates if value is not None)


def _write_l0_entrypoint(project: Path, intent: str) -> None:
    reason = "This repository does not expose a packageable algorithm capability for the requested intent."
    (project / "main.py").write_text(
        '"""IOEB template entrypoint for an explicit unsupported negative control."""\n\n'
        "from __future__ import annotations\n\n\n"
        "def main_process(request: dict[str, object]) -> dict[str, object]:\n"
        '    """Reject a request that the source repository cannot implement.\n\n'
        "    Args:\n"
        "        request: JSON-compatible request payload.\n\n"
        "    Returns:\n"
        "        This function never returns successfully.\n"
        '    """\n'
        f"    raise RuntimeError({reason!r})\n",
        encoding="utf-8",
    )
    if not (project / "requirements.txt").exists():
        (project / "requirements.txt").write_text("", encoding="utf-8")
    readme = project / "README.ioeb.md"
    with readme.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n## Negative-control behavior\n\n"
            "This is an explicit unsupported entrypoint. It does not claim or fake algorithm "
            "functionality absent from the source repository.\n\n"
            f"Requested intent: {intent}\n"
        )


def _human_git_identity() -> tuple[str, str]:
    def read(key: str) -> str:
        result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else ""

    name, email = read("user.name"), read("user.email")
    lowered = f"{name} {email}".lower()
    if not name or not email or "codex" in lowered or "openai" in lowered:
        raise RuntimeError("a configured human git user.name/user.email is required for derived repositories")
    return name, email


def _commit_derived_repo(project: Path, sample_id: str) -> str:
    name, email = _human_git_identity()
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
    )
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True, timeout=30, env=env)
    subprocess.run(["git", "add", "--all"], cwd=project, check=True, timeout=60, env=env)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", f"Adapt {sample_id} to IOEB template"],
        cwd=project,
        check=True,
        timeout=120,
        env=env,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_readme(project: Path, sample: dict[str, Any], source_sha: str) -> None:
    (project / "README.ioeb.md").write_text(
        "# IOEB template adaptation\n\n"
        "This directory is a derived copy prepared for template-compliant evaluation. "
        "The original AMQ-Bench repository and sample are unchanged.\n\n"
        f"- Sample: `{sample['sample_id']}`\n"
        f"- Source commit: `{sample['repo_info']['commit_sha']}`\n"
        f"- Source tree SHA-256: `{source_sha}`\n"
        f"- Requested intent: {sample['wrap_intent']}\n\n"
        "The adaptation Agent received only the requested intent and repository source. "
        "It did not receive the benchmark task, ground truth, or verification script.\n",
        encoding="utf-8",
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _protected_digest(sample: dict[str, Any]) -> str:
    return _json_digest(
        {key: value for key, value in sample.items() if key not in {"repo_info", "template_adaptation"}}
    )


def recover_last_template_writes(run_dir: Path) -> dict[str, str]:
    event_path = run_dir / "events.jsonl"
    recovered: dict[str, str] = {}
    if not event_path.is_file():
        return recovered
    for line in event_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "tool_call" or event.get("data", {}).get("tool") != "write_template_file":
            continue
        arguments = event.get("data", {}).get("arguments", {})
        path = arguments.get("path")
        content = arguments.get("content")
        if path in {"main.py", "requirements.txt"} and isinstance(content, str) and content.strip():
            recovered[path] = content.rstrip() + "\n"
    return recovered


async def adapt_one(
    sample: dict[str, Any],
    *,
    output_root: Path,
    cache_roots: list[Path],
    max_attempts: int,
    resume: bool,
) -> dict[str, Any]:
    sample_id = sample["sample_id"]
    run_dir = output_root / "adaptation" / sample_id
    repo_dir = output_root / "adapted_repos" / sample_id
    derived_path = run_dir / "derived_sample.json"
    summary_path = run_dir / "summary.json"
    if resume and derived_path.is_file() and summary_path.is_file() and (repo_dir / ".git").is_dir():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("status") == "ready":
            resumed_report = validate_algorithm_template(
                repo_dir,
                allow_explicit_unsupported=bool(summary.get("negativeControl")),
            )
            if resumed_report.passed:
                print(f"[{sample_id}] resume: ready", flush=True)
                return json.loads(derived_path.read_text(encoding="utf-8"))
            print(
                f"[{sample_id}] resume: stale validation, regenerating",
                flush=True,
            )

    recovered_writes = recover_last_template_writes(run_dir) if resume else {}
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.rmtree(repo_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    summary: dict[str, Any] = {
        "sampleId": sample_id,
        "status": "failed",
        "constructionInput": "source_and_wrap_intent_only",
        "protectedFieldsDigest": _protected_digest(sample),
    }

    try:
        with tempfile.TemporaryDirectory(prefix=f".{sample_id}-", dir=repo_dir.parent) as temp_dir:
            source = Path(temp_dir) / "source"
            staged = Path(temp_dir) / "adapted"
            cache_source, clone_seconds = await asyncio.to_thread(prepare_source, sample, source, cache_roots)
            source_tree_sha = await asyncio.to_thread(_tree_digest, source)
            await asyncio.to_thread(shutil.copytree, source, staged, ignore=COPY_IGNORE)
            summary.update(
                {
                    "repositorySource": cache_source,
                    "cloneSeconds": round(clone_seconds, 3),
                    "originalCommit": sample["repo_info"]["commit_sha"],
                    "originalTreeSha256": source_tree_sha,
                }
            )

            original_main: str | None = None
            if (staged / "main.py").exists():
                original_main = "ioeb_original_main.py"
                if (staged / original_main).exists():
                    raise RuntimeError(f"cannot preserve original main.py because {original_main} already exists")
                (staged / "main.py").replace(staged / original_main)
            if (staged / "requirements.txt").exists():
                shutil.copy2(staged / "requirements.txt", staged / "requirements.original.txt")
            _write_readme(staged, sample, source_tree_sha)

            is_l0 = _is_l0(sample)
            errors: list[str] = []
            if is_l0:
                _write_l0_entrypoint(staged, sample["wrap_intent"])
            else:
                for relative, content in recovered_writes.items():
                    (staged / relative).write_text(content, encoding="utf-8")
                recovered_report = validate_algorithm_template(staged)
                if recovered_writes:
                    _write_json_atomic(run_dir / "validation_recovered.json", recovered_report.to_dict())
                    summary["recoveredFromPriorRun"] = True
                if recovered_report.passed:
                    summary["adaptationAttempts"] = 0
                else:
                    errors = recovered_report.errors
                event_path = run_dir / "events.jsonl"
                for attempt in range(1, max_attempts + 1) if errors else ():
                    ir = await asyncio.to_thread(RepositoryAnalyzer().analyze, staged)
                    agent = build_template_adapter_agent(staged, ir)
                    prompt = template_adapter_prompt(ir, sample["wrap_intent"], original_main)
                    if errors:
                        prompt += "\n上一次确定性校验错误，请全部修复：\n" + "\n".join(errors)
                    with event_path.open("a", encoding="utf-8") as handle:
                        async for event in agent.run(prompt):
                            handle.write(
                                json.dumps({"attempt": attempt, **event.to_dict()}, ensure_ascii=False, default=str)
                                + "\n"
                            )
                    report = validate_algorithm_template(staged)
                    _write_json_atomic(run_dir / f"validation_attempt_{attempt}.json", report.to_dict())
                    errors = report.errors
                    if report.passed:
                        summary["adaptationAttempts"] = attempt
                        break

            report = validate_algorithm_template(staged, allow_explicit_unsupported=is_l0)
            _write_json_atomic(run_dir / "validation.json", report.to_dict())
            if not report.passed:
                raise RuntimeError("template validation failed: " + "; ".join(report.errors))

            metadata = {
                "schemaVersion": "ioeb.amq-template-adaptation/v1",
                "sampleId": sample_id,
                "createdAt": datetime.now().astimezone().isoformat(),
                "constructionInput": "source_and_wrap_intent_only",
                "benchmarkFieldsHiddenFromAgent": ["task", "ground_truth", "evaluation_criteria"],
                "originalRepoInfo": sample["repo_info"],
                "originalTreeSha256": source_tree_sha,
                "negativeControl": is_l0,
                "validation": report.to_dict(),
            }
            _write_json_atomic(staged / "template_adaptation.json", metadata)
            staged.rename(repo_dir)
            commit = await asyncio.to_thread(_commit_derived_repo, repo_dir, sample_id)

        derived = dict(sample)
        derived["repo_info"] = {"url": repo_dir.resolve().as_uri(), "commit_sha": commit}
        derived["template_adaptation"] = {
            "schemaVersion": "ioeb.amq-template-adaptation/v1",
            "originalRepoInfo": sample["repo_info"],
            "originalTreeSha256": summary["originalTreeSha256"],
            "constructionInput": "source_and_wrap_intent_only",
            "negativeControl": is_l0,
        }
        if _protected_digest(derived) != summary["protectedFieldsDigest"]:
            raise RuntimeError("protected benchmark fields changed during adaptation")
        _write_json_atomic(derived_path, derived)
        summary.update(
            {
                "status": "ready",
                "derivedCommit": commit,
                "templatePassed": True,
                "negativeControl": is_l0,
            }
        )
        return derived
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        summary["totalSeconds"] = round(time.perf_counter() - started, 3)
        _write_json_atomic(summary_path, summary)
        print(f"[{sample_id}] {summary['status']} seconds={summary['totalSeconds']}", flush=True)


async def main() -> int:
    args = parse_args()
    if args.concurrency < 1 or args.max_attempts < 1:
        raise SystemExit("--concurrency and --max-attempts must be >= 1")
    benchmark_file = args.benchmark_file.resolve()
    output_root = args.output_root.resolve()
    ensure_output_outside_source_repo(benchmark_file, output_root)
    samples = load_mini30(benchmark_file)
    original_sha = sha256_file(benchmark_file)
    output_root.mkdir(parents=True, exist_ok=True)
    cache_roots = [path.resolve() for path in args.repo_cache_root]
    if not cache_roots:
        cache_roots = [Path(os.getenv("REPO_CACHE_ROOT", ".repo_cache")).resolve()]

    protocol = {
        "schemaVersion": "ioeb.amq-template-subset/v1",
        "sourceBenchmark": str(benchmark_file),
        "sourceBenchmarkSha256": original_sha,
        "sampleCount": len(samples),
        "constructionInput": "source_and_wrap_intent_only",
        "protectedFields": list(PROTECTED_FIELDS),
        "concurrency": args.concurrency,
    }
    _write_json_atomic(output_root / "adaptation_protocol.json", protocol)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(sample: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await adapt_one(
                sample,
                output_root=output_root,
                cache_roots=cache_roots,
                max_attempts=args.max_attempts,
                resume=args.resume,
            )

    results = await asyncio.gather(*(bounded(sample) for sample in samples), return_exceptions=True)
    failures = {
        samples[index]["sample_id"]: f"{type(result).__name__}: {result}"
        for index, result in enumerate(results)
        if isinstance(result, BaseException)
    }
    successful = {
        result["sample_id"]: result for result in results if isinstance(result, dict)
    }
    ordered = [successful[sample["sample_id"]] for sample in samples if sample["sample_id"] in successful]
    target = output_root / "data" / (
        "mini30_template_adapted.jsonl" if not failures else "mini30_template_adapted.partial.jsonl"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in ordered), encoding="utf-8"
    )
    source_unchanged = sha256_file(benchmark_file) == original_sha
    summary = {
        **protocol,
        "complete": not failures and len(ordered) == EXPECTED_MINI30_SIZE,
        "successfulSamples": len(ordered),
        "failedSamples": failures,
        "derivedBenchmark": str(target),
        "derivedBenchmarkSha256": sha256_file(target),
        "sourceBenchmarkUnchanged": source_unchanged,
    }
    _write_json_atomic(output_root / "adaptation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["complete"] and source_unchanged else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
