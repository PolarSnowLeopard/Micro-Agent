"""仿真构建持久化：一份事实轨迹、一份验收轨迹、一份运行产物。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from micro_agent.core.config import config
from micro_agent.simulation.artifact_compiler import (
    build_accepted_trajectory,
    build_meta_app_artifact,
    stable_hash,
)


BUILD_ROOT = Path(config.workspace) / "data" / "simulation_builds"
_PART_FILES = {
    "manifest": "manifest.json",
    "trace": "trace.json",
    "accepted_trajectory": "accepted_trajectory.json",
    "artifact": "artifact.json",
}


def build_ref(build_id: str) -> dict[str, str]:
    base = f"/api/simulation/{build_id}"
    return {
        "buildId": build_id,
        "manifestUrl": f"{base}/manifest",
        "traceUrl": f"{base}/trace",
        "acceptedTrajectoryUrl": f"{base}/accepted-trajectory",
        "artifactUrl": f"{base}/artifact",
        "runUrl": f"{base}/run",
        "experimentUrl": f"{base}/experiments/run",
    }


def _is_publishable(
    trace: dict[str, Any],
    accepted: dict[str, Any],
    artifact: dict[str, Any],
) -> bool:
    iteration = accepted.get("acceptedIteration")
    bindings = (artifact.get("runtime") or {}).get("serviceBindings") or []
    return all((
        trace.get("terminalStatus") == "SUCCEEDED",
        trace.get("success") is True,
        trace.get("cancelled") is not True,
        accepted.get("status") == "accepted",
        isinstance(iteration, int) and iteration > 0,
        bool(accepted.get("actionSequence")),
        bool(artifact.get("artifactId")),
        bool(bindings),
    ))


class BuildBundleStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or BUILD_ROOT

    def bundle_dir(self, build_id: str) -> Path:
        return self.root / build_id

    def save_from_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        build_id = str(trace.get("build_id") or trace.get("session_id") or "")
        if not build_id:
            raise ValueError("trace missing build_id/session_id")

        accepted = build_accepted_trajectory(trace)
        artifact = build_meta_app_artifact(trace, accepted)
        artifact_hash = stable_hash(artifact)
        accepted["generatedArtifact"] = {
            "artifactId": artifact.get("artifactId"),
            "artifactHash": artifact_hash,
            "recordedAt": datetime.now(timezone.utc).isoformat(),
        }

        bundle = self.bundle_dir(build_id)
        bundle.mkdir(parents=True, exist_ok=True)
        self._write_json(bundle / "trace.json", trace)
        self._write_json(bundle / "accepted_trajectory.json", accepted)
        self._write_json(bundle / "artifact.json", artifact)

        manifest = {
            "schemaVersion": "simulation_build_bundle.v1",
            "buildId": build_id,
            "artifactId": artifact.get("artifactId"),
            "artifactHash": artifact_hash,
            "terminalStatus": trace.get("terminalStatus"),
            "publishable": _is_publishable(trace, accepted, artifact),
            "paths": dict(_PART_FILES),
            "researchEligible": self._research_eligible(trace, artifact),
            "ref": build_ref(build_id),
        }
        self._write_manifest(bundle / "manifest.json", manifest)
        return manifest

    def list_builds(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        return [
            manifest
            for path in sorted(self.root.iterdir(), reverse=True)
            if path.is_dir()
            and (manifest := self.load_part(path.name, "manifest"))
        ]

    def load_part(self, build_id: str, part: str) -> dict[str, Any] | None:
        path = self.bundle_dir(build_id) / _PART_FILES.get(part, part)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save_experiment_result(self, build_id: str, result: dict[str, Any]) -> Path:
        path = self.bundle_dir(build_id) / "experiment" / "latest_result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(path, result)
        return path

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_manifest(path: Path, data: Any) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _research_eligible(trace: dict[str, Any], artifact: dict[str, Any]) -> bool:
        bindings = (artifact.get("runtime") or {}).get("serviceBindings") or []
        calls = [
            event.get("data") or {}
            for event in trace.get("events", [])
            if event.get("type") == "tool_call_record"
        ]
        return bool(bindings) and all(
            binding.get("source") == "real_mcp" for binding in bindings
        ) and any(call.get("source") == "real_mcp" for call in calls)


__all__ = ["BuildBundleStore", "BUILD_ROOT", "build_ref"]
