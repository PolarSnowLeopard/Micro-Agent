"""仿真构建路由：会话管理 + SSE 事件流 + 研究记录。

端点与 ioeb design_docs/build-design4llm.md §5 一致：
  POST   /api/simulation/start
  GET    /api/simulation/{id}/stream   (SSE)
  POST   /api/simulation/{id}/cancel
  GET    /api/simulation/{id}/result
  GET    /api/simulation/records
  POST   /api/simulation/records/compare
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from micro_agent.simulation import service

router = APIRouter(prefix="/api/simulation", tags=["simulation"])


class CompareRequest(BaseModel):
    recordIds: List[str] = []


@router.post("/start")
async def simulation_start(request: Request):
    body = await request.json()
    return service.start_session(body)


@router.get("/{session_id}/stream")
async def simulation_stream(session_id: str):
    return StreamingResponse(
        service.iter_sse(session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/cancel")
async def simulation_cancel(session_id: str):
    service.cancel_session(session_id)
    return {"success": True}


@router.get("/{session_id}/result")
async def simulation_result(session_id: str):
    return service.get_result(session_id)


@router.get("/records")
async def simulation_records():
    return service.list_records()


@router.post("/records/compare")
async def simulation_records_compare(req: CompareRequest):
    return service.compare_records(req.recordIds)
