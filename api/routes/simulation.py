"""Meta-app scenario simulation construction routes.

Each simulation session produces one BuildBundle under
workspace/data/simulation_builds/{buildId}.
"""

from __future__ import annotations

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
from micro_agent.simulation.orchestrator import SimulationEvent, SimulationOrchestrator
from micro_agent.simulation.trace_records import (
    build_tool_call_record_events,
    build_trace_metadata,
)

router = APIRouter(prefix="/api/agent/simulation", tags=["simulation"])

# 与 ioeb simulation_builder_data.SIMULATION_BUILD_GEN_TASKS 一致
_GEN_PREP_TASKS = ("汇总数据", "编译产物", "准备发布")

_sessions: dict[str, dict[str, Any]] = {}
_store = BuildBundleStore()


class SimulationStartRequest(BaseModel):
    appId: str = ""
    appName: str = "元应用"
    domain: str = "generic"
    servicesMeta: list[dict] = Field(default_factory=list)
    maxIterations: int = 5
    scenarioDescription: str = ""
    scenarioParsed: dict = Field(default_factory=dict)


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
    _sessions[build_id] = {"config": req.model_dump(), "orchestrator": None}
    return {
        "success": True,
        "sessionId": build_id,
        "buildId": build_id,
        "streamUrl": f"/api/agent/simulation/{build_id}/stream",
        "buildRef": build_ref(build_id),
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
        terminal_event: SimulationEvent | None = None
        save_attempted = False

        def make_trace() -> dict[str, Any]:
            terminal = terminal_event.data if terminal_event else {}
            metrics = terminal.get("metrics") or {}
            success = bool(terminal.get("success"))
            cancelled = bool(terminal.get("cancelled"))
            try:
                tool_events = build_tool_call_record_events(orchestrator.call_records())
            except Exception as exc:
                logger.warning(f"collect tool call records failed: {exc}")
                tool_events = []
            return {
                "schemaVersion": "build_trace.v1",
                "build_id": build_id,
                "session_id": build_id,
                "app_name": cfg.get("appName", ""),
                "domain": cfg.get("domain", ""),
                "events": trace_events + tool_events,
                "success": success,
                "cancelled": cancelled,
                "terminalStatus": "CANCELLED" if cancelled else "SUCCEEDED" if success else "FAILED",
                "iterations": int(metrics.get("iterations") or 0),
                "elapsed_ms": int(metrics.get("elapsedMs") or 0),
                "metadata": build_trace_metadata(cfg, len(tool_events)),
            }

        try:
            async for event in orchestrator.run():
                trace_events.append(event.to_dict())
                if event.type == "complete":
                    terminal_event = event
                    continue
                yield event.to_sse()

            if terminal_event is None:
                terminal_event = SimulationEvent("complete", {
                    "success": False,
                    "metrics": {"iterations": 0, "elapsedMs": 0},
                    "result": {"error": "构建未产生终止事件"},
                })
                trace_events.append(terminal_event.to_dict())

            yield SimulationEvent("step", {"step": 3, "name": "方案生成"}).to_sse()
            for index, text in enumerate(_GEN_PREP_TASKS):
                yield SimulationEvent(
                    "progress",
                    {"ctx": "gen", "index": index, "text": text, "active": True},
                ).to_sse()
                yield SimulationEvent(
                    "progress",
                    {"ctx": "gen", "index": index, "text": text, "done": True},
                ).to_sse()

            save_attempted = True
            try:
                manifest = _store.save_from_trace(make_trace())
                logger.info(f"BuildBundle saved: {manifest['buildId']}")
            except Exception as exc:
                logger.error(f"BuildBundle save failed: {exc}", exc_info=True)
                terminal_event.data.update({
                    "success": False,
                    "publishable": False,
                    "buildId": build_id,
                })
                terminal_event.data["result"] = {
                    "error": f"构建产物保存失败: {exc}",
                    "suggestion": "请检查 Micro-Agent 工作区后重新构建",
                }
            else:
                success = bool(terminal_event.data.get("success"))
                publishable = bool(manifest.get("publishable"))
                terminal_event.data.update({
                    "success": success,
                    "publishable": publishable,
                    "buildId": build_id,
                    "artifactId": manifest.get("artifactId"),
                    "artifactHash": manifest.get("artifactHash"),
                    "buildRef": manifest.get("ref") or build_ref(build_id),
                })
                if success and not publishable:
                    terminal_event.data["publishError"] = {
                        "error": "构建完成，但产物不满足预发布条件",
                        "suggestion": "请检查最终 Verifier、AcceptedTrajectory 与服务绑定",
                    }

            yield terminal_event.to_sse()
        finally:
            if not save_attempted:
                try:
                    _store.save_from_trace(make_trace())
                except Exception as exc:
                    logger.error(f"partial BuildBundle save failed: {exc}", exc_info=True)
            _sessions.pop(build_id, None)

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


@router.get("/{build_id}/manifest")
async def get_build_manifest(build_id: str):
    return _load(build_id, "manifest")


@router.get("/{build_id}/trace")
async def get_build_trace(build_id: str):
    return _load(build_id, "trace")


@router.get("/{build_id}/accepted-trajectory")
async def get_accepted_trajectory(build_id: str):
    return _load(build_id, "accepted_trajectory")


@router.get("/{build_id}/artifact")
async def get_build_artifact(build_id: str):
    return _load(build_id, "artifact")


@router.post("/{build_id}/run")
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


@router.post("/{build_id}/experiments/run")
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


@router.post("/{build_id}/evidence")
async def get_evidence(build_id: str):
    manifest = _load(build_id, "manifest")
    accepted = _load(build_id, "accepted_trajectory")
    artifact = _load(build_id, "artifact")
    bindings = (artifact.get("runtime") or {}).get("serviceBindings") or []
    return {
        "schemaVersion": "build_evidence_summary.v1",
        "overallStatus": "PASS" if accepted.get("status") == "accepted" else "WARN",
        "summary": {
            "acceptedTrajectory": accepted.get("status"),
            "selectedServices": len(bindings),
            "researchEligible": manifest.get("researchEligible"),
        },
        "missingEvidence": [] if accepted.get("status") == "accepted" else ["accepted_trajectory"],
    }


def _load(build_id: str, part: str) -> dict[str, Any]:
    data = _store.load_part(build_id, part)
    if data is None:
        raise HTTPException(404, f"{part} not found for build {build_id}")
    return data
