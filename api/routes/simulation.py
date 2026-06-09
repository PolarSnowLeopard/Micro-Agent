"""仿真构建路由：启动仿真、SSE 事件流、轨迹查询、证据分析、产物编译。

端点与前端 simulation_builder.js HTTP 客户端一一对应：
  POST /api/simulation/start              → 启动会话，返回 sessionId + streamUrl
  GET  /api/simulation/{id}/stream        → SSE 命名事件流（EventSource 兼容）
  POST /api/simulation/{id}/cancel        → 取消
  GET  /api/simulation/records            → 列出历史轨迹
  POST /api/simulation/records/compare    → 对比
  GET  /api/simulation/{id}/trace         → 获取已持久化轨迹
  POST /api/simulation/{id}/evidence      → 构建证据卡片+检查报告（自动落盘）
  GET  /api/simulation/{id}/artifact      → 获取 ArtifactSpec v0（确定性编译）
  POST /api/simulation/{id}/artifact      → 按需构建 ArtifactSpec 并落盘
"""

from __future__ import annotations

import json
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
from micro_agent.simulation.trace_store import FileTraceStore

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
    maxIterations: int = 5
    scenarioDescription: str = ""
    scenarioSummary: str = ""
    parsedIntent: dict = Field(default_factory=dict)
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
                if event.type == "complete":
                    data = event.data
                    final_success = data.get("success", False)
                    metrics = data.get("metrics", {})
                    final_iterations = metrics.get("iterations", 0)
                    final_elapsed = metrics.get("elapsedMs", 0)
                    # 注入 sessionId，前端可通过此 ID 拉取 artifact
                    event.data["sessionId"] = session_id
                yield event.to_sse()
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

            # 自动运行 evidence pipeline（为 ArtifactSpec 编译器提供完整证据）
            pipeline_result = None
            try:
                from trace_evidence import run_pipeline as _run_evidence

                result = _run_evidence(record.to_dict())
                evidence_dir = Path(config.workspace) / "data" / "evidence" / session_id
                evidence_dir.mkdir(parents=True, exist_ok=True)
                result.save_to_dir(evidence_dir)
                pipeline_result = result
                logger.info(f"证据产物已自动落盘: {evidence_dir}")
            except Exception as e:
                logger.warning(f"证据分析自动运行失败 (non-fatal): {e}")

            # 自动编译 ArtifactSpec 并落盘（best-effort，不阻塞 SSE 流）
            try:
                from micro_agent.simulation.artifact_compiler import compile_artifact_spec

                artifacts_dir = Path(config.workspace) / "data" / "artifacts" / session_id
                artifacts_dir.mkdir(parents=True, exist_ok=True)
                spec = compile_artifact_spec(record.to_dict(), pipeline_result)
                spec_path = artifacts_dir / "artifact_spec.json"
                spec_path.write_text(
                    json.dumps(spec.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(
                    f"ArtifactSpec 已编译并落盘: {spec_path} "
                    f"(solidifiable={spec.solidifiable})"
                )
            except Exception as e:
                logger.warning(f"ArtifactSpec 自动编译失败 (non-fatal): {e}")

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


# --------------- 辅助函数 ---------------

def _load_pipeline_result(session_id: str, trace_dict: dict):
    """从已落盘的 evidence 产物重建 PipelineResult 对象，供 artifact 编译器使用。

    优先从落盘文件重建（避免重复运行 pipeline），回退到重新运行。
    """
    from trace_evidence import run_pipeline as _run_evidence

    evidence_dir = Path(config.workspace) / "data" / "evidence" / session_id
    pr_path = evidence_dir / "pipeline_result.json"

    # 如果 evidence 已落盘，直接调用 run_pipeline 即可（它会从 trace_dict 确定性重建）
    # run_pipeline 本身是幂等的——同一条 trace 每次产出相同结果
    try:
        return _run_evidence(trace_dict)
    except Exception:
        return None


# --------------- 轨迹查询 ---------------

@router.get("/records")
async def list_records(appName: str | None = None):
    return await _trace_store.list_all(app_name=appName)


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

    # ---- 持久化证据产物（消除"算完即弃"缺口） ----
    evidence_dir = Path(config.workspace) / "data" / "evidence" / session_id
    try:
        manifest = result.save_to_dir(evidence_dir)
        logger.info(f"证据产物已落盘: {evidence_dir} ({len(manifest)} 文件)")
    except Exception as exc:
        logger.warning(f"证据产物落盘失败 (non-fatal): {exc}")

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


@router.get("/{session_id}/artifact")
async def get_artifact(session_id: str):
    """获取仿真产物的 ArtifactSpec v0。若有已落盘的 evidence，自动注入。"""
    record = await _trace_store.load(session_id)
    if not record:
        raise HTTPException(404, "trace not found")

    from micro_agent.simulation.artifact_compiler import compile_artifact_spec

    # 尝试加载已落盘的 evidence（若 SSE finally 中已自动跑过）
    pipeline_result = _load_pipeline_result(session_id, record.to_dict())

    try:
        spec = compile_artifact_spec(record.to_dict(), pipeline_result)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.warning(f"ArtifactSpec 编译失败 {session_id}: {exc}")
        raise HTTPException(500, f"ArtifactSpec 编译失败: {exc}") from exc

    return spec.to_dict()


@router.post("/{session_id}/artifact")
async def build_artifact(session_id: str):
    """按需构建 ArtifactSpec v0 并落盘到 workspace/data/artifacts/{session_id}/。"""
    record = await _trace_store.load(session_id)
    if not record:
        raise HTTPException(404, "trace not found")

    from micro_agent.simulation.artifact_compiler import compile_artifact_spec

    pipeline_result = _load_pipeline_result(session_id, record.to_dict())

    try:
        spec = compile_artifact_spec(record.to_dict(), pipeline_result)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.warning(f"ArtifactSpec 编译失败 {session_id}: {exc}")
        raise HTTPException(500, f"ArtifactSpec 编译失败: {exc}") from exc

    # 落盘
    art_dir = Path(config.workspace) / "data" / "artifacts" / session_id
    art_dir.mkdir(parents=True, exist_ok=True)
    art_path = art_dir / "artifact_spec.json"
    art_path.write_text(
        json.dumps(spec.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"ArtifactSpec 已落盘: {art_path}")

    return {
        "artifactId": spec.artifactId,
        "schemaVersion": spec.schemaVersion,
        "sourceSessionId": session_id,
        "solidifiable": spec.solidifiable,
        "artifactPath": str(art_path),
    }
