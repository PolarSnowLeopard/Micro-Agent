"""Read-only adapter that creates leakage-aware suites from AMQ-Bench data."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


SUITE_VERSION = "ioeb.amq-template-suite/v1"
TRUSTED_VERIFY_TIERS = {"specific_numeric", "domain_structural", "exact_string"}


def _fingerprint(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not value.get("sample_id"):
            raise ValueError(f"invalid AMQ sample at {path}:{line_number}")
        rows.append(value)
    return rows


def _load_id_set(path: Optional[Path]) -> Set[str]:
    if path is None:
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        for candidate in value.values():
            if isinstance(candidate, list):
                return {str(item) for item in candidate}
    raise ValueError(f"development ID file must contain a JSON list: {path}")


def _sample_summary(sample: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
    repo = sample.get("repo_info") or {}
    intent = sample.get("wrap_intent", "")
    return {
        "sampleId": sample["sample_id"],
        "source": (sample.get("tags") or ["unknown"])[0],
        "category": repo.get("category"),
        "difficulty": sample.get("difficulty") or (sample.get("task") or {}).get("difficulty"),
        "verifyTier": status.get("verify_tier"),
        "repository": {
            "url": repo.get("url"),
            "commit": repo.get("commit_sha"),
        },
        "intentHash": f"sha256:{hashlib.sha256(intent.encode('utf-8')).hexdigest()}",
    }


def _is_negative_control(sample: Dict[str, Any]) -> bool:
    category = str((sample.get("repo_info") or {}).get("category", ""))
    sample_id = str(sample.get("sample_id", ""))
    return category.startswith("L0") or sample_id.startswith("meb_l0_")


def prepare_amq_suite(
    dataset: Path | str,
    status_file: Path | str,
    *,
    development_ids: Path | str | None = None,
) -> Dict[str, Any]:
    """Select strong no-secret positives and isolate all non-development items as holdout."""
    dataset_path = Path(dataset).expanduser().resolve()
    status_path = Path(status_file).expanduser().resolve()
    development_path = Path(development_ids).expanduser().resolve() if development_ids else None
    samples = _load_jsonl(dataset_path)
    statuses = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(statuses, dict):
        raise ValueError("AMQ sample status must be a JSON object keyed by sample_id")
    dev_ids = _load_id_set(development_path)

    development: List[Dict[str, Any]] = []
    holdout: List[Dict[str, Any]] = []
    negative: List[Dict[str, Any]] = []
    excluded_reasons: Counter[str] = Counter()
    for sample in sorted(samples, key=lambda item: item["sample_id"]):
        sample_id = sample["sample_id"]
        status = statuses.get(sample_id) or {}
        if _is_negative_control(sample):
            item = _sample_summary(sample, status)
            item["requiresManualAudit"] = True
            negative.append(item)
            continue
        if status.get("verify_tier") not in TRUSTED_VERIFY_TIERS:
            excluded_reasons["weak_or_unknown_oracle"] += 1
            continue
        if sample.get("required_env"):
            excluded_reasons["requires_external_environment"] += 1
            continue
        item = _sample_summary(sample, status)
        if sample_id in dev_ids:
            development.append(item)
        else:
            holdout.append(item)

    development_repositories = {
        item["repository"]["url"] for item in development
    }
    leaked = [
        item for item in holdout if item["repository"]["url"] in development_repositories
    ]
    if leaked:
        leaked_ids = {item["sampleId"] for item in leaked}
        holdout = [item for item in holdout if item["sampleId"] not in leaked_ids]
        development.extend(leaked)
        development.sort(key=lambda item: item["sampleId"])

    return {
        "suiteVersion": SUITE_VERSION,
        "policy": {
            "track": "ioeb-template",
            "trustedVerifyTiers": sorted(TRUSTED_VERIFY_TIERS),
            "requiresNoExternalEnvironment": True,
            "strictDeterministicOracle": True,
            "splitByRepository": True,
            "holdoutContentMustNotEnterPrompts": True,
        },
        "inputs": {
            "dataset": str(dataset_path),
            "datasetFingerprint": _fingerprint(dataset_path),
            "status": str(status_path),
            "statusFingerprint": _fingerprint(status_path),
            "developmentIds": str(development_path) if development_path else None,
            "developmentIdsFingerprint": _fingerprint(development_path) if development_path else None,
        },
        "splits": {
            "development": development,
            "holdout": holdout,
            "negative": negative,
        },
        "summary": {
            "sourceSamples": len(samples),
            "development": len(development),
            "holdout": len(holdout),
            "negative": len(negative),
            "excluded": sum(excluded_reasons.values()),
            "excludedReasons": dict(sorted(excluded_reasons.items())),
        },
    }
