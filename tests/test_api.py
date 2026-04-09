"""Phase 3 测试：TaskManager + API 路由。

运行方式：
    cd Micro-Agent
    python -m tests.test_api
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_task_context():
    """测试 TaskContext 的事件存储和订阅。"""
    from core.task import TaskContext
    from core.schema import AgentEvent

    ctx = TaskContext(task_id="test1")
    assert ctx.status == "running"

    await ctx.add_event(AgentEvent(type="think", step=1, data={"thought": "hi"}))
    await ctx.add_event(AgentEvent(type="done", step=1, data={"result": "ok"}))
    assert len(ctx.events) == 2

    # 订阅全部事件
    ctx.status = "completed"
    events = []
    async for e in ctx.subscribe(after=0):
        events.append(e)
    assert len(events) == 2
    assert events[0].type == "think"
    assert events[1].type == "done"

    # 断线续传：从 after=1 开始
    events2 = []
    async for e in ctx.subscribe(after=1):
        events2.append(e)
    assert len(events2) == 1
    assert events2[0].type == "done"

    print("[PASS] TaskContext 事件存储与订阅")


async def test_task_manager_cancel():
    """测试 TaskManager 的提交和取消。"""
    from core.task import TaskManager
    from core.agent import Agent
    from core.llm import LLM
    from core.config import LLMConfig
    from tool.registry import ToolRegistry
    from tool.terminate import Terminate

    mgr = TaskManager()
    llm = LLM(LLMConfig())
    tools = ToolRegistry()
    tools.register(Terminate())
    agent = Agent(name="test", llm=llm, tools=tools)

    # 提交后立即取消
    ctx = await mgr.submit(agent, "test", task_id="cancel_test")
    assert ctx.status == "running"
    mgr.cancel("cancel_test")
    assert ctx.status == "cancelled"

    # 等一下让后台 task 处理取消
    await asyncio.sleep(0.5)

    # 验证事件里有 cancelled
    has_cancelled = any(
        e.data.get("reason") == "cancelled" for e in ctx.events
    )
    assert has_cancelled, f"Events: {[e.to_dict() for e in ctx.events]}"

    # 列出任务
    tasks = mgr.list_tasks()
    assert len(tasks) >= 1
    assert any(t["task_id"] == "cancel_test" for t in tasks)

    print("[PASS] TaskManager 提交与取消")


async def test_task_manager_subscribe_realtime():
    """测试实时订阅：后台产生事件，前台实时收到。"""
    from core.task import TaskContext
    from core.schema import AgentEvent

    ctx = TaskContext(task_id="rt_test")
    received = []

    async def producer():
        await asyncio.sleep(0.1)
        await ctx.add_event(AgentEvent(type="think", step=1, data={"thought": "a"}))
        await asyncio.sleep(0.1)
        await ctx.add_event(AgentEvent(type="done", step=1, data={"result": "b"}))
        ctx.status = "completed"

    async def consumer():
        async for e in ctx.subscribe():
            received.append(e)

    # producer 和 consumer 并发运行
    await asyncio.gather(producer(), consumer())
    assert len(received) == 2
    assert received[0].data["thought"] == "a"
    assert received[1].data["result"] == "b"

    print("[PASS] TaskContext 实时订阅")


async def test_fastapi_import():
    """测试 FastAPI app 能正常导入。"""
    from api.app import app
    assert app.title == "Micro-Agent V2"

    routes = [r.path for r in app.routes]
    assert "/api/tasks" in routes or any("/api/tasks" in r for r in routes)
    assert "/health" in routes

    print("[PASS] FastAPI 应用导入")


async def main():
    await test_task_context()
    await test_task_manager_subscribe_realtime()
    await test_task_manager_cancel()
    await test_fastapi_import()
    print("\n=== ALL PHASE 3 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
