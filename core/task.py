"""任务管理层：解耦 Agent 执行与 SSE 推送。

核心设计：
- 任务提交后在后台 asyncio task 中独立执行，不依赖 HTTP 连接
- 事件存储在内存 buffer 中，支持任意时刻查询历史
- subscribe() 支持断线续传（通过 after 参数跳过已读事件）
- 多个订阅者可同时观看同一任务
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from loguru import logger

from core.agent import Agent
from core.schema import AgentEvent


@dataclass
class TaskContext:
    """单个任务的运行上下文。"""

    task_id: str
    status: str = "running"  # running | completed | failed | cancelled
    events: list[AgentEvent] = field(default_factory=list)
    _agent: Optional[Agent] = field(default=None, repr=False)
    _condition: asyncio.Condition = field(
        default_factory=asyncio.Condition, repr=False
    )

    async def add_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        async with self._condition:
            self._condition.notify_all()

    async def subscribe(self, after: int = 0) -> AsyncIterator[AgentEvent]:
        """订阅事件流。after = 上次已读的事件数量，用于断线续传。"""
        idx = after
        while True:
            # 推送所有未读事件
            while idx < len(self.events):
                yield self.events[idx]
                idx += 1
            # 任务已结束，不再等待
            if self.status != "running":
                break
            # 等待新事件或超时（超时后重新检查状态）
            async with self._condition:
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass

    def cancel(self) -> None:
        if self._agent:
            self._agent.cancel()


class TaskManager:
    """管理所有任务的生命周期。

    用法：
        manager = TaskManager()
        ctx = await manager.submit("task1", agent, "请分析代码")
        
        # SSE 端点
        async for event in ctx.subscribe():
            yield event.to_sse()
        
        # 状态查询
        ctx = manager.get("task1")
        print(ctx.status, len(ctx.events))
    """

    def __init__(self, max_tasks: int = 100) -> None:
        self._tasks: dict[str, TaskContext] = {}
        self._max_tasks = max_tasks

    async def submit(
        self,
        agent: Agent,
        request: str,
        task_id: Optional[str] = None,
    ) -> TaskContext:
        """提交任务并在后台执行。返回 TaskContext。"""
        task_id = task_id or uuid.uuid4().hex[:12]

        if len(self._tasks) >= self._max_tasks:
            self._cleanup_finished()

        ctx = TaskContext(task_id=task_id, _agent=agent)
        self._tasks[task_id] = ctx
        asyncio.create_task(self._execute(ctx, request))
        logger.info(f"任务 {task_id} 已提交")
        return ctx

    async def _execute(self, ctx: TaskContext, request: str) -> None:
        try:
            async for event in ctx._agent.run(request):
                await ctx.add_event(event)
            ctx.status = "completed"
        except Exception as e:
            logger.error(f"任务 {ctx.task_id} 执行失败: {e}")
            await ctx.add_event(
                AgentEvent(type="error", step=0, data={"error": str(e)})
            )
            ctx.status = "failed"
        finally:
            if ctx._agent and hasattr(ctx._agent, "memory"):
                try:
                    await ctx._agent.memory.persist()
                except Exception as e:
                    logger.warning(f"记忆持久化失败: {e}")
            logger.info(f"任务 {ctx.task_id} 结束，状态: {ctx.status}")

    def get(self, task_id: str) -> Optional[TaskContext]:
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        ctx = self._tasks.get(task_id)
        if ctx and ctx.status == "running":
            ctx.cancel()
            ctx.status = "cancelled"
            return True
        return False

    def list_tasks(self) -> list[dict]:
        return [
            {
                "task_id": ctx.task_id,
                "status": ctx.status,
                "event_count": len(ctx.events),
            }
            for ctx in self._tasks.values()
        ]

    def _cleanup_finished(self) -> None:
        """清理已完成的任务，为新任务腾出空间。"""
        finished = [
            tid
            for tid, ctx in self._tasks.items()
            if ctx.status in ("completed", "failed", "cancelled")
        ]
        for tid in finished[: len(finished) // 2]:
            del self._tasks[tid]
