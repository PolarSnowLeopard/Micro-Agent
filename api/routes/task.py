"""任务管理路由：提交、查询、SSE 流、取消。

端点设计：
  POST   /api/tasks              → 提交任务，返回 task_id
  GET    /api/tasks              → 列出所有任务
  GET    /api/tasks/{id}/status  → 查询任务状态 + 全部事件
  GET    /api/tasks/{id}/stream  → SSE 事件流（支持 Last-Event-ID 断线续传）
  POST   /api/tasks/{id}/cancel  → 取消任务
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.deps import build_agent, task_manager
from tool.mcp.connection import ServerConfig

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskSubmitRequest(BaseModel):
    """任务提交请求。"""

    task_id: Optional[str] = None
    prompt: str
    agent_name: str = "agent"
    system_prompt: str = ""
    next_step_prompt: str = ""
    max_steps: Optional[int] = None
    use_mcp: bool = False
    mcp_servers: Optional[list[dict]] = None


@router.post("")
async def submit_task(req: TaskSubmitRequest):
    """提交一个 Agent 任务，在后台执行。"""
    agent, _ = await build_agent(
        name=req.agent_name,
        system_prompt=req.system_prompt,
        next_step_prompt=req.next_step_prompt,
        max_steps=req.max_steps,
        use_mcp=req.use_mcp,
    )

    # 如果是 MCP agent 且提供了服务器配置，先连接
    if req.use_mcp and req.mcp_servers:
        from core.mcp_agent import MCPAgent

        assert isinstance(agent, MCPAgent)
        for srv in req.mcp_servers:
            await agent.connect(ServerConfig(**srv))

    ctx = await task_manager.submit(agent, req.prompt, task_id=req.task_id)
    return {"task_id": ctx.task_id, "status": ctx.status}


@router.get("")
async def list_tasks():
    """列出所有任务。"""
    return task_manager.list_tasks()


@router.get("/{task_id}/status")
async def get_task_status(task_id: str):
    """查询任务状态 + 全部事件。用户切换页面后回来调这个接口恢复进度。"""
    ctx = task_manager.get(task_id)
    if not ctx:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return {
        "task_id": ctx.task_id,
        "status": ctx.status,
        "event_count": len(ctx.events),
        "events": [e.to_dict() for e in ctx.events],
    }


@router.get("/{task_id}/stream")
async def stream_task_events(task_id: str, request: Request):
    """SSE 事件流。支持 Last-Event-ID 断线续传。"""
    ctx = task_manager.get(task_id)
    if not ctx:
        raise HTTPException(404, f"任务 {task_id} 不存在")

    # 读取 Last-Event-ID header，实现断线续传
    last_id = request.headers.get("Last-Event-ID", "0")
    after = int(last_id) if last_id.isdigit() else 0

    async def generate():
        idx = after
        async for event in ctx.subscribe(after=after):
            yield f"id: {idx}\n{event.to_sse()}"
            idx += 1

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消正在执行的任务。"""
    if task_manager.cancel(task_id):
        return {"task_id": task_id, "status": "cancelled"}
    ctx = task_manager.get(task_id)
    if not ctx:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return {"task_id": task_id, "status": ctx.status, "message": "任务不在运行中"}
