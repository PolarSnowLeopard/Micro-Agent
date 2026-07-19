"""Phase 2 测试：MCP 连接管理 + MCPAgent + Bash 工具。

运行方式：
    cd Micro-Agent
    python -m tests.test_mcp
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_bash_tool():
    """测试 Bash 工具基本功能。"""
    from micro_agent.tool.bash import Bash

    bash = Bash(timeout=10)
    result = await bash.execute(command="echo hello && echo world")
    assert "hello" in result.output, f"Unexpected: {result}"
    assert "world" in result.output, f"Unexpected: {result}"

    result = await bash.execute(command="pwd")
    assert result.output, f"pwd should return something: {result}"

    result = await bash.execute(command="ls /nonexistent_path_12345")
    assert result.error or "No such file" in str(result), f"Should report error: {result}"

    print("[PASS] Bash 工具")


async def test_tool_registry_namespace():
    """测试 ToolRegistry 的 namespace 注册/注销。"""
    from micro_agent.tool.registry import ToolRegistry
    from micro_agent.tool.terminate import Terminate
    from micro_agent.tool.bash import Bash

    reg = ToolRegistry()
    reg.register(Terminate())
    reg.register(Bash(), namespace="server1")
    reg.register(Terminate(), namespace="server1")

    assert "terminate" in reg
    assert "server1_bash" in reg
    assert "server1_terminate" in reg
    assert len(reg) == 3

    llm_tools = reg.to_llm_format()
    names = [t["function"]["name"] for t in llm_tools]
    assert "server1_bash" in names

    removed = reg.unregister_by_namespace("server1")
    assert len(removed) == 2
    assert "server1_bash" not in reg
    assert "terminate" in reg  # 非 namespace 的不受影响

    print("[PASS] ToolRegistry namespace")


async def test_mcp_connection_manager_init():
    """测试 MCPConnectionManager 基础功能（不连接真实服务器）。"""
    from micro_agent.tool.mcp.connection import MCPConnectionManager

    mgr = MCPConnectionManager()
    assert mgr.server_ids() == []

    # 测试 async with
    async with MCPConnectionManager() as conn:
        assert conn.server_ids() == []

    print("[PASS] MCPConnectionManager 基础")


async def test_mcp_connections_close_in_reverse_order():
    """AnyIO transport scopes must unwind in reverse connection order."""
    from micro_agent.tool.mcp.connection import MCPConnectionManager

    closed = []

    class Stack:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            closed.append(self.name)

    mgr = MCPConnectionManager()
    mgr._sessions = {"first": object(), "second": object()}
    mgr._stacks = {"first": Stack("first"), "second": Stack("second")}
    await mgr.disconnect_all()

    assert closed == ["second", "first"]


async def test_mcp_agent_init():
    """测试 MCPAgent 初始化（不连接真实服务器）。"""
    from micro_agent.core.mcp_agent import MCPAgent
    from micro_agent.core.llm import LLM
    from micro_agent.core.config import LLMConfig

    llm = LLM(LLMConfig())

    async with MCPAgent(llm=llm) as agent:
        assert "terminate" in agent.tools
        assert agent._connected == []
        assert agent.max_steps == 40

    print("[PASS] MCPAgent 初始化")


async def test_agent_cancel():
    """测试 Agent 的 cancel / reset 机制。"""
    from micro_agent.core.agent import Agent
    from micro_agent.core.llm import LLM
    from micro_agent.core.config import LLMConfig
    from micro_agent.tool.registry import ToolRegistry
    from micro_agent.tool.terminate import Terminate

    llm = LLM(LLMConfig())
    tools = ToolRegistry()
    tools.register(Terminate())

    agent = Agent(name="test", llm=llm, tools=tools)

    # cancel() 后 run() 应立即返回 cancelled
    agent.cancel()
    events = []
    async for event in agent.run("test"):
        events.append(event)
    assert len(events) == 1
    assert events[0].type == "done"
    assert events[0].data["reason"] == "cancelled"

    # reset() 后 cancel 标记应清除
    agent.reset()
    assert not agent._cancelled

    print("[PASS] Agent cancel")


async def test_agent_requires_terminal_tool_after_plain_text_response():
    from micro_agent.core.agent import Agent
    from micro_agent.core.llm import LLMResponse
    from micro_agent.core.schema import ToolCall
    from micro_agent.tool.base import Tool, ToolResult
    from micro_agent.tool.registry import ToolRegistry

    class Finalize(Tool):
        name = "finalize"
        description = "Finalize the deterministic task."
        parameters = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

        async def execute(self, **kwargs):
            return ToolResult(output="finished")

    class PlainThenToolLLM:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="I would make the change.")
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="final-call",
                        name="finalize",
                        arguments="{}",
                    )
                ]
            )

    registry = ToolRegistry()
    registry.register(Finalize())
    llm = PlainThenToolLLM()
    agent = Agent(
        llm=llm,
        tools=registry,
        terminal_tools={"finalize"},
        require_terminal_tool=True,
        max_steps=3,
    )

    events = [event async for event in agent.run("finish with a tool")]

    assert llm.calls == 2
    assert any(
        event.type == "think"
        and "终止工具契约" in event.data.get("thought", "")
        for event in events
    )
    assert events[-1].type == "done"
    assert events[-1].data["tool"] == "finalize"


async def test_agent_blocks_identical_tool_calls_before_provider_history_repeats():
    from micro_agent.core.agent import Agent
    from micro_agent.core.llm import LLMResponse
    from micro_agent.core.schema import ToolCall
    from micro_agent.tool.base import Tool, ToolResult
    from micro_agent.tool.registry import ToolRegistry

    class CountingTool(Tool):
        name = "count_once"
        description = "Count one deterministic invocation."
        parameters = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        }

        def __init__(self):
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return ToolResult(output=str(kwargs["value"]))

    class RepeatingLLM:
        def __init__(self):
            self.calls = 0
            self.histories = []

        async def complete(self, messages, **kwargs):
            self.calls += 1
            self.histories.append(messages)
            if self.calls <= 2:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call-{self.calls}",
                            name="count_once",
                            arguments='{"value": 1}',
                        )
                    ]
                )
            return LLMResponse(content="used the existing result")

    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = RepeatingLLM()
    agent = Agent(llm=llm, tools=registry, max_steps=4)

    events = [event async for event in agent.run("run once")]

    assert tool.calls == 1
    assert any(
        event.type == "think" and "重复工具调用阻断" in event.data.get("thought", "")
        for event in events
    )
    assert events[-1].type == "done"
    assert events[-1].data["result"] == "used the existing result"
    assistant_tool_messages = [
        message
        for message in llm.histories[-1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert len(assistant_tool_messages) == 1


async def main():
    await test_bash_tool()
    await test_tool_registry_namespace()
    await test_mcp_connection_manager_init()
    await test_mcp_agent_init()
    await test_agent_cancel()
    await test_agent_blocks_identical_tool_calls_before_provider_history_repeats()
    print("\n=== ALL PHASE 2 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
