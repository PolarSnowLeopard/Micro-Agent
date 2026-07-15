"""Sequential, reproducible batch evaluation for IoEB Template Track cases."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any, Dict, List

from mcp_packager.engine import build_package
from mcp_packager.quality import aggregate_quality, score_verification
from mcp_packager.verifier import verify_artifact_docker, verify_artifact_static


BATCH_VERSION = "ioeb.amq-template-batch/v2"


def discover_template_cases(root: Path | str) -> List[Path]:
    """Return production-contract case directories in stable path order."""
    source = Path(root).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"batch root must be a directory: {source}")
    if (source / "ioeb_algorithm.json").is_file():
        return [source]
    return sorted(
        {manifest.parent for manifest in source.rglob("ioeb_algorithm.json")},
        key=lambda path: path.relative_to(source).as_posix(),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _mean_duration(samples: List[Dict[str, Any]], name: str) -> float:
    values = [sample["durationsSeconds"].get(name, 0.0) for sample in samples]
    return round(mean(values), 4) if values else 0.0


def _sample_id(path: Path, manifest: Dict[str, Any]) -> str:
    benchmark = manifest.get("benchmark") or {}
    service = manifest.get("service") or {}
    return benchmark.get("sampleId") or service.get("name") or path.name


def _expected_disposition(manifest: Dict[str, Any]) -> tuple[str, List[str]]:
    benchmark = manifest.get("benchmark") or {}
    disposition = benchmark.get("expectedDisposition", "accept")
    if disposition not in {"accept", "reject"}:
        disposition = "accept"
    issue_codes = benchmark.get("expectedIssueCodes") or []
    expected_codes = sorted(
        {code for code in issue_codes if isinstance(code, str) and code}
    )
    return disposition, expected_codes


def run_template_batch(
    root: Path | str,
    *,
    docker: bool = False,
    build_timeout: int = 600,
    startup_timeout: int = 60,
    execution_timeout: int = 120,
    no_cache: bool = False,
) -> Dict[str, Any]:
    """Build and verify every discovered case without repair or LLM fallback."""
    root_path = Path(root).expanduser().resolve()
    cases = discover_template_cases(root_path)
    samples: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="ioeb-amq-template-batch-") as temp_dir:
        artifacts_root = Path(temp_dir)
        for index, case in enumerate(cases, start=1):
            build_started = perf_counter()
            validation, plan, artifact = build_package(
                case,
                artifacts_root / f"case-{index:04d}",
                strict=True,
            )
            build_duration = round(perf_counter() - build_started, 4)
            sample: Dict[str, Any] = {
                "sampleId": _sample_id(case, validation.manifest),
                "source": case.relative_to(root_path).as_posix()
                if case != root_path
                else ".",
                "sourceHash": validation.source_hash,
                "validation": validation.to_dict(),
                "firstPass": True,
                "repairAttempts": 0,
                "durationsSeconds": {
                    "packageBuild": build_duration,
                    "verification": 0.0,
                    "total": build_duration,
                },
            }
            expected_disposition, expected_issue_codes = _expected_disposition(
                validation.manifest
            )
            observed_issue_codes = sorted(
                {issue.code for issue in validation.issues}
            )
            sample.update(
                {
                    "expectedDisposition": expected_disposition,
                    "expectedIssueCodes": expected_issue_codes,
                    "observedIssueCodes": observed_issue_codes,
                }
            )
            if expected_disposition == "reject":
                expectation_met = (
                    not validation.valid
                    and set(expected_issue_codes).issubset(observed_issue_codes)
                )
                sample.update(
                    {
                        "success": expectation_met,
                        "expectationMet": expectation_met,
                        "failureStage": None if expectation_met else "negative_control",
                        "verification": None,
                        "quality": None,
                    }
                )
                samples.append(sample)
                continue
            if artifact is None or plan is None:
                sample.update(
                    {
                        "success": False,
                        "expectationMet": False,
                        "failureStage": "validation",
                        "verification": None,
                        "quality": None,
                    }
                )
                samples.append(sample)
                continue

            verification_started = perf_counter()
            if docker:
                verification = verify_artifact_docker(
                    artifact,
                    build_timeout=build_timeout,
                    startup_timeout=startup_timeout,
                    execution_timeout=execution_timeout,
                    no_cache=no_cache,
                )
            else:
                verification = verify_artifact_static(artifact)
            verification_duration = round(perf_counter() - verification_started, 4)
            quality = score_verification(verification)
            if not verification.get("success"):
                failure_stage = "verification"
            elif docker and not quality.get("qualityGatePassed"):
                failure_stage = "quality_gate"
            else:
                failure_stage = None
            sample["verification"] = verification
            sample["quality"] = quality
            sample["failureStage"] = failure_stage
            sample["success"] = failure_stage is None
            sample["expectationMet"] = failure_stage is None
            sample["durationsSeconds"].update(
                {
                    "verification": verification_duration,
                    "total": round(build_duration + verification_duration, 4),
                }
            )
            samples.append(sample)

    positive_samples = [
        sample for sample in samples if sample["expectedDisposition"] == "accept"
    ]
    negative_samples = [
        sample for sample in samples if sample["expectedDisposition"] == "reject"
    ]
    qualities = [
        sample["quality"]
        for sample in positive_samples
        if isinstance(sample.get("quality"), dict)
    ]
    total = len(samples)
    positive_total = len(positive_samples)
    negative_total = len(negative_samples)
    validation_passes = sum(
        sample["validation"]["valid"] for sample in positive_samples
    )
    verification_passes = sum(
        bool(sample.get("verification", {}).get("success"))
        for sample in positive_samples
        if isinstance(sample.get("verification"), dict)
    )
    successes = sum(bool(sample["success"]) for sample in samples)
    positive_successes = sum(bool(sample["success"]) for sample in positive_samples)
    negative_successes = sum(bool(sample["success"]) for sample in negative_samples)
    summary = {
        "samples": total,
        "positiveSamples": positive_total,
        "negativeSamples": negative_total,
        "succeeded": successes,
        "failed": total - successes,
        "firstPassSuccessRate": _ratio(successes, total),
        "acceptancePassRate": _ratio(positive_successes, positive_total),
        "rejectionPassRate": _ratio(negative_successes, negative_total),
        "validationPassRate": _ratio(validation_passes, positive_total),
        "verificationPassRate": _ratio(verification_passes, positive_total),
        "publishableRate": _ratio(
            sum(bool(quality.get("publishable")) for quality in qualities),
            positive_total,
        ),
        "qualityGatePassRate": _ratio(
            sum(bool(quality.get("qualityGatePassed")) for quality in qualities),
            positive_total,
        ),
        "inputValidationGatePassRate": _ratio(
            sum(
                quality.get("inputValidationGate", {}).get("passed") is True
                for quality in qualities
            ),
            positive_total,
        ),
        "meanPackageBuildSeconds": _mean_duration(
            positive_samples, "packageBuild"
        ),
        "meanVerificationSeconds": _mean_duration(
            positive_samples, "verification"
        ),
        "meanTotalSeconds": _mean_duration(positive_samples, "total"),
        "meanRejectionSeconds": _mean_duration(negative_samples, "total"),
        "failureStages": {
            stage: sum(sample.get("failureStage") == stage for sample in samples)
            for stage in sorted(
                {
                    sample.get("failureStage")
                    for sample in samples
                    if sample.get("failureStage")
                }
            )
        },
    }
    return {
        "batchVersion": BATCH_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "mode": "docker" if docker else "static",
        "dockerCachePolicy": (
            "disabled" if docker and no_cache else "default" if docker else None
        ),
        "root": str(root_path),
        "success": total > 0 and successes == total,
        "summary": summary,
        "aggregateQuality": aggregate_quality(qualities),
        "samples": samples,
    }
