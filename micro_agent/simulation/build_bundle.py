"""BuildBundle storage for meta-app simulation construction.

New simulation builds are stored as one directory per build. This is the only
new persistence unit for the simulation-construction module; old trace/artifact
folders are intentionally not read or migrated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from micro_agent.core.config import config
from micro_agent.simulation.artifact_compiler import (
    attach_artifact_hash_to_accepted,
    compile_build,
    stable_hash,
)


BUILD_ROOT = Path(config.workspace) / "data" / "simulation_builds"


def build_ref(build_id: str) -> dict[str, str]:
    base = f"/api/simulation/builds/{build_id}"
    return {
        "buildId": build_id,
        "manifestUrl": f"{base}/manifest",
        "traceUrl": f"{base}/trace",
        "serviceSelectionUrl": f"{base}/service-selection",
        "acceptedTrajectoryUrl": f"{base}/accepted-trajectory",
        "artifactUrl": f"{base}/artifact",
        "frontendStateUrl": f"{base}/frontend-state",
        "runUrl": f"{base}/run",
        "experimentUrl": f"{base}/experiments/run",
    }


class BuildBundleStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or BUILD_ROOT

    def bundle_dir(self, build_id: str) -> Path:
        return self.root / build_id

    def exists(self, build_id: str) -> bool:
        return (self.bundle_dir(build_id) / "manifest.json").exists()

    def save_from_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        build_id = str(trace.get("build_id") or trace.get("session_id") or "")
        if not build_id:
            raise ValueError("trace missing build_id/session_id")

        compiled = compile_build(trace)
        bundle = self.bundle_dir(build_id)
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "experiment").mkdir(exist_ok=True)

        artifact = compiled.artifact
        artifact_hash = stable_hash(artifact)
        accepted = attach_artifact_hash_to_accepted(
            compiled.acceptedTrajectory,
            artifact_id=str(artifact.get("artifactId") or ""),
            artifact_hash=artifact_hash,
        )
        frontend = dict(compiled.frontendState)
        frontend["acceptedTrajectorySummary"] = {
            **(frontend.get("acceptedTrajectorySummary") or {}),
            "generatedArtifact": accepted.get("generatedArtifact") or {},
        }

        self._write_json(bundle / "trace.json", trace)
        self._write_json(bundle / "service_selection.json", compiled.serviceSelection)
        self._write_json(bundle / "accepted_trajectory.json", accepted)
        self._write_json(bundle / "artifact.json", artifact)
        self._write_json(bundle / "frontend_state.json", frontend)

        manifest = {
            "schemaVersion": "simulation_build_bundle.v1",
            "buildId": build_id,
            "artifactId": artifact.get("artifactId"),
            "paths": {
                "trace": "trace.json",
                "serviceSelection": "service_selection.json",
                "acceptedTrajectory": "accepted_trajectory.json",
                "artifact": "artifact.json",
                "frontendState": "frontend_state.json",
                "experimentDir": "experiment",
            },
            "hashes": {
                "trace": stable_hash(trace),
                "serviceSelection": stable_hash(compiled.serviceSelection),
                "acceptedTrajectory": stable_hash(accepted),
                "artifact": artifact_hash,
                "frontendState": stable_hash(frontend),
            },
            "researchEligible": self._research_eligible(trace, artifact),
            "ref": build_ref(build_id),
        }
        self._write_json(bundle / "manifest.json", manifest)
        return manifest

    def list_builds(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        rows = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            manifest = self.load_part(path.name, "manifest")
            if manifest:
                rows.append(manifest)
        return rows

    def load_part(self, build_id: str, part: str) -> dict[str, Any] | None:
        filename = {
            "manifest": "manifest.json",
            "trace": "trace.json",
            "service_selection": "service_selection.json",
            "accepted_trajectory": "accepted_trajectory.json",
            "artifact": "artifact.json",
            "frontend_state": "frontend_state.json",
        }.get(part, part)
        path = self.bundle_dir(build_id) / filename
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_experiment_result(self, build_id: str, result: dict[str, Any]) -> Path:
        exp_dir = self.bundle_dir(build_id) / "experiment"
        exp_dir.mkdir(parents=True, exist_ok=True)
        path = exp_dir / "latest_result.json"
        self._write_json(path, result)
        return path

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _research_eligible(trace: dict[str, Any], artifact: dict[str, Any]) -> bool:
        bindings = (artifact.get("runtime") or {}).get("serviceBindings") or []
        if not bindings:
            return False
        if any(b.get("source") != "real_mcp" for b in bindings):
            return False
        calls = [
            e.get("data") for e in trace.get("events", [])
            if e.get("type") == "tool_call_record"
        ]
        return any((c or {}).get("source") == "real_mcp" for c in calls)


__all__ = ["BuildBundleStore", "BUILD_ROOT", "build_ref"]
