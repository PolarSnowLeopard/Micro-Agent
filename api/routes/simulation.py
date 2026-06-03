"""仿真构建路由：启动仿真、SSE 事件流、轨迹查询。

端点与前端 simulation_builder.js HTTP 客户端一一对应：
  POST /api/simulation/start           → 启动会话，返回 sessionId + streamUrl
  GET  /api/simulation/{id}/stream     → SSE 命名事件流（EventSource 兼容）
  POST /api/simulation/{id}/cancel     → 取消
  GET  /api/simulation/records         → 列出历史轨迹
  POST /api/simulation/records/compare → 对比
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from micro_agent.core.config import config
from micro_agent.simulation.orchestrator import SimulationOrchestrator
from micro_agent.simulation.trace_records import (
    build_tool_call_record_events,
    build_trace_metadata,
)
from micro_agent.simulation.trace_store import FileTraceStore, TraceRecord

router = APIRouter(prefix="/api/simulation", tags=["simulation"])

_sessions: dict[str, dict[str, Any]] = {}

_trace_store = FileTraceStore(Path(config.workspace) / "data" / "traces")


# --------------- Models ---------------

class SimulationStartRequest(BaseModel):
    appId: str = ""
    appName: str = "元应用"
    domain: str = "generic"
    serviceIds: list[str] = Field(default_factory=list)
    servicesMeta: list[dict] = Field(default_factory=list)
    maxIterations: int = 3
    scenarioDescription: str = ""
    mode: str = "production"
    strategy: dict = Field(default_factory=dict)


class CompareRequest(BaseModel):
    recordIds: list[str]


# --------------- 启动 ---------------

@router.post("/start")
async def start_simulation(req: SimulationStartRequest):
    session_id = f"sim-{uuid.uuid4().hex[:12]}"
    _sessions[session_id] = {
        "config": req.dict(),
        "orchestrator": None,
    }
    logger.info(f"仿真会话已创建: {session_id}")
    return {
        "success": True,
        "sessionId": session_id,
        "streamUrl": f"/api/simulation/{session_id}/stream",
    }


# --------------- SSE 流 ---------------

@router.get("/{session_id}/stream")
async def simulation_stream(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "session not found")

    orchestrator = SimulationOrchestrator(session["config"])
    session["orchestrator"] = orchestrator

    trace_events: list[dict] = []
    cfg = session["config"]

    async def generate():
        final_success = False
        final_iterations = 0
        final_elapsed = 0

        try:
            async for event in orchestrator.run():
                trace_events.append(event.to_dict())
                yield event.to_sse()
                if event.type == "complete":
                    data = event.data
                    final_success = data.get("success", False)
                    metrics = data.get("metrics", {})
                    final_iterations = metrics.get("iterations", 0)
                    final_elapsed = metrics.get("elapsedMs", 0)
        finally:
            tool_call_events: list[dict] = []
            try:
                tool_call_events = build_tool_call_record_events(
                    orchestrator._collect_call_records()
                )
            except Exception as e:
                logger.debug(f"收集 tool_call_records 失败 (non-fatal): {e}")

            metadata = build_trace_metadata(cfg, len(tool_call_events))

            # 合并 tool_call_record 到 trace_events 末尾
            all_events = trace_events + tool_call_events

            record = TraceRecord(
                session_id=session_id,
                app_name=cfg.get("appName", ""),
                domain=cfg.get("domain", ""),
                mode=cfg.get("mode", "production"),
                strategy=cfg.get("strategy", {}),
                events=all_events,
                success=final_success,
                iterations=final_iterations,
                elapsed_ms=final_elapsed,
                metadata=metadata,
            )
            try:
                await _trace_store.save(record)
            except Exception as e:
                logger.warning(f"保存轨迹失败: {e}")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-ID": session_id,
        },
    )


# --------------- 取消 ---------------

@router.post("/{session_id}/cancel")
async def cancel_simulation(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "session not found")
    orch = session.get("orchestrator")
    if orch:
        orch.cancel()
    return {"success": True}


# --------------- 轨迹查询 ---------------

@router.get("/records")
async def list_records():
    return await _trace_store.list_all()


@router.post("/records/compare")
async def compare_records(req: CompareRequest):
    records = await _trace_store.compare(req.recordIds)
    return {"records": records}


@router.get("/{session_id}/trace")
async def get_trace(session_id: str):
    record = await _trace_store.load(session_id)
    if not record:
        raise HTTPException(404, "trace not found")
    return record.to_dict()


@router.post("/{session_id}/evidence")
async def build_evidence(session_id: str):
    record = await _trace_store.load(session_id)
    if not record:
        raise HTTPException(404, "trace not found")
    try:
        from trace_evidence import run_pipeline

        result = run_pipeline(record.to_dict())
    except Exception as exc:
        logger.warning(f"证据分析失败 {session_id}: {exc}")
        raise HTTPException(422, str(exc)) from exc

    report = result.report
    from trace_evidence.evidence_checker import summarize_evidence_dimensions

    def _check_api(c):
        return {
            "checkName": c.check_name,
            "status": c.status,
            "detail": (c.detail or "")[:240],
            "category": c.category,
        }

    checks = [_check_api(c) for c in report.checks]
    non_pass = [c for c in checks if c["status"] != "PASS"]
    return {
        "evidenceId": result.card.evidence_id,
        "overallStatus": report.overall_status,
        "summary": report.summary,
        "checks": checks,
        "failedChecks": non_pass,
        "dimensions": summarize_evidence_dimensions(report.checks),
        "cardSummary": result.card.summary,
        "verification": result.card.verification,
        "missingEvidence": result.bundle.missing_evidence,
    }
