"""Meta-app scenario simulation construction routes.

New contract: one simulation session produces one BuildBundle under
workspace/data/simulation_builds/{buildId}. Old trace/artifact/evidence folders
are not read or migrated.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from micro_agent.simulation.artifact_runtime import run_artifact
from micro_agent.simulation.build_bundle import BuildBundleStore, build_ref
from micro_agent.simulation.experiments import (
    list_experiment_runners,
    run_experiment_for_build,
)
from micro_agent.simulation.orchestrator import SimulationOrchestrator
from micro_agent.simulation.trace_records import (
    build_tool_call_record_events,
    build_trace_metadata,
)

router = APIRouter(prefix="/api/simulation", tags=["simulation"])

_sessions: dict[str, dict[str, Any]] = {}
_store = BuildBundleStore()


class SimulationStartRequest(BaseModel):
    appId: str = ""
    appName: str = "元应用"
    domain: str = "generic"
    serviceIds: list[str] = Field(default_factory=list)
    servicesMeta: list[dict] = Field(default_factory=list)
    maxIterations: int = 5
    scenarioDescription: str = ""
    scenarioSummary: str = ""
    scenarioParsed: dict = Field(default_factory=dict)
    mode: str = "production"
    strategy: dict = Field(default_factory=dict)


class ExperimentRunRequest(BaseModel):
    tasks: list[dict] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)


class ArtifactRunRequest(BaseModel):
    message: str
    preferGoldenPath: bool = True


class CompareRequest(BaseModel):
    recordIds: list[str] = Field(default_factory=list)


@router.post("/start")
async def start_simulation(req: SimulationStartRequest):
    build_id = f"build-{uuid.uuid4().hex[:12]}"
    _sessions[build_id] = {"config": req.dict(), "orchestrator": None}
    return {
        "success": True,
        "sessionId": build_id,
        "buildId": build_id,
        "streamUrl": f"/api/simulation/{build_id}/stream",
        "buildRef": build_ref(build_id),
        "artifactRef": build_ref(build_id),
    }


@router.get("/{build_id}/stream")
async def simulation_stream(build_id: str):
    session = _sessions.get(build_id)
    if not session:
        raise HTTPException(404, "session not found")

    orchestrator = SimulationOrchestrator(session["config"])
    session["orchestrator"] = orchestrator
    trace_events: list[dict[str, Any]] = []
    cfg = session["config"]

    async def generate():
        final_success = False
        final_iterations = 0
        final_elapsed = 0

        try:
            async for event in orchestrator.run():
                trace_events.append(event.to_dict())
                if event.type == "complete":
                    final_success = bool(event.data.get("success"))
                    metrics = event.data.get("metrics") or {}
                    final_iterations = int(metrics.get("iterations") or 0)
                    final_elapsed = int(metrics.get("elapsedMs") or 0)
                    ref = build_ref(build_id)
                    event.data["buildId"] = build_id
                    event.data["buildRef"] = ref
                    event.data["artifactRef"] = ref
                    result = event.data.get("result")
                    if isinstance(result, dict):
                        result["buildId"] = build_id
                        result["buildRef"] = ref
                        result["artifactRef"] = ref
                yield event.to_sse()
        finally:
            try:
                tool_events = build_tool_call_record_events(orchestrator._collect_call_records())
            except Exception as exc:
                logger.warning(f"collect tool call records failed: {exc}")
                tool_events = []

            trace = {
                "schemaVersion": "build_trace.v1",
                "build_id": build_id,
                "session_id": build_id,
                "app_name": cfg.get("appName", ""),
                "domain": cfg.get("domain", ""),
                "mode": cfg.get("mode", "production"),
                "strategy": cfg.get("strategy", {}),
                "events": trace_events + tool_events,
                "success": final_success,
                "iterations": final_iterations,
                "elapsed_ms": final_elapsed,
                "metadata": build_trace_metadata(cfg, len(tool_events)),
            }
            try:
                manifest = _store.save_from_trace(trace)
                logger.info(f"BuildBundle saved: {manifest['buildId']}")
            except Exception as exc:
                logger.error(f"BuildBundle save failed: {exc}", exc_info=True)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Build-ID": build_id},
    )


@router.post("/{build_id}/cancel")
async def cancel_simulation(build_id: str):
    session = _sessions.get(build_id)
    if not session:
        raise HTTPException(404, "session not found")
    orch = session.get("orchestrator")
    if orch:
        orch.cancel()
    return {"success": True}


@router.get("/records")
async def list_records():
    return {"records": _store.list_builds()}


@router.post("/records/compare")
async def compare_records(req: CompareRequest):
    records = []
    for build_id in req.recordIds:
        item = _store.load_part(build_id, "manifest")
        if item:
            records.append(item)
    return {"records": records}


@router.get("/builds")
async def list_builds():
    return {"builds": _store.list_builds()}


@router.get("/builds/{build_id}/manifest")
async def get_build_manifest(build_id: str):
    return _load(build_id, "manifest")


@router.get("/builds/{build_id}/trace")
async def get_build_trace(build_id: str):
    return _load(build_id, "trace")


@router.get("/builds/{build_id}/service-selection")
async def get_service_selection(build_id: str):
    return _load(build_id, "service_selection")


@router.get("/builds/{build_id}/accepted-trajectory")
async def get_accepted_trajectory(build_id: str):
    return _load(build_id, "accepted_trajectory")


@router.get("/builds/{build_id}/artifact")
async def get_build_artifact(build_id: str):
    return _load(build_id, "artifact")


@router.get("/builds/{build_id}/frontend-state")
async def get_frontend_state(build_id: str):
    return _load(build_id, "frontend_state")


@router.post("/builds/{build_id}/run")
async def run_build_artifact(build_id: str, req: ArtifactRunRequest):
    artifact = _load(build_id, "artifact")
    try:
        return await run_artifact(
            artifact,
            req.message,
            prefer_golden_path=req.preferGoldenPath,
        )
    except Exception as exc:
        logger.warning(f"artifact run failed {build_id}: {exc}")
        raise HTTPException(422, str(exc)) from exc


@router.get("/experiments/runners")
async def get_experiment_runners():
    return {"runners": list_experiment_runners()}


@router.post("/builds/{build_id}/experiments/run")
async def run_build_experiment(build_id: str, req: ExperimentRunRequest):
    if not req.tasks:
        raise HTTPException(400, "tasks is required")
    try:
        return await run_experiment_for_build(
            build_id,
            req.tasks,
            baselines=req.baselines or None,
            store=_store,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        logger.warning(f"experiment run failed {build_id}: {exc}")
        raise HTTPException(422, str(exc)) from exc


# Temporary compatibility URLs for the current frontend. They read the new
# BuildBundle and do not support old trace/artifact storage.

@router.get("/{build_id}/trace")
async def get_trace(build_id: str):
    return _load(build_id, "trace")


@router.post("/{build_id}/evidence")
async def get_evidence(build_id: str):
    manifest = _load(build_id, "manifest")
    accepted = _load(build_id, "accepted_trajectory")
    selection = _load(build_id, "service_selection")
    return {
        "schemaVersion": "build_evidence_summary.v1",
        "overallStatus": "PASS" if accepted.get("status") == "accepted" else "WARN",
        "summary": {
            "acceptedTrajectory": accepted.get("status"),
            "selectedServices": len(selection.get("selectedServices") or []),
            "researchEligible": manifest.get("researchEligible"),
        },
        "missingEvidence": [] if accepted.get("status") == "accepted" else ["accepted_trajectory"],
    }


@router.get("/{build_id}/artifact")
async def get_artifact(build_id: str):
    return _load(build_id, "artifact")


@router.get("/{build_id}/frontend-state")
async def get_legacy_frontend_state(build_id: str):
    return _load(build_id, "frontend_state")


@router.post("/{build_id}/artifact")
async def rebuild_artifact(build_id: str):
    manifest = _load(build_id, "manifest")
    return {
        "buildId": build_id,
        "artifactId": manifest.get("artifactId"),
        "artifactPath": str(_store.bundle_dir(build_id) / "artifact.json"),
        "manifest": manifest,
    }


def _load(build_id: str, part: str) -> dict[str, Any]:
    data = _store.load_part(build_id, part)
    if data is None:
        raise HTTPException(404, f"{part} not found for build {build_id}")
    return data
