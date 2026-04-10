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
    from micro_agent.tool.mcp.connection import MCPConnectionManager, ServerConfig

    mgr = MCPConnectionManager()
    assert mgr.server_ids() == []

    # 测试 async with
    async with MCPConnectionManager() as conn:
        assert conn.server_ids() == []

    print("[PASS] MCPConnectionManager 基础")


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


async def main():
    await test_bash_tool()
    await test_tool_registry_namespace()
    await test_mcp_connection_manager_init()
    await test_mcp_agent_init()
    await test_agent_cancel()
    print("\n=== ALL PHASE 2 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
