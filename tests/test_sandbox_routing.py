"""SandboxTool 路由边界：只有显式 isFake 服务可以进入模拟通道。"""

from __future__ import annotations

import asyncio

import pytest

from micro_agent.simulation.service_tool_session import (
    ServiceConnectionError,
    ServiceToolSession,
)
from micro_agent.simulation.orchestrator import SimulationOrchestrator


def _session(service: dict, **kwargs) -> ServiceToolSession:
    return ServiceToolSession([service], **kwargs)


def test_explicit_fake_service_registers_sandbox_tool():
    session = _session({
        "id": "fake-service",
        "name": "Fake Service",
        "isFake": True,
    })

    asyncio.run(session.connect())

    assert session.statuses()[0]["channel"] == "sandbox"
    assert "fake-service_execute" in session.tools.list_names()


def test_orchestrator_builds_agents_from_connected_service_statuses():
    orchestrator = SimulationOrchestrator({
        "servicesMeta": [{"id": "fake-service", "name": "Fake Service", "isFake": True}],
    })

    async def prepare():
        return [event async for event in orchestrator._prepare_services()]

    asyncio.run(prepare())
    prompt = orchestrator._planner_system_prompt()
    planner = orchestrator._build_planner()
    verifier = orchestrator._build_verifier()

    assert "Fake Service (sandbox)" in prompt
    assert "fake-service_execute" in prompt
    assert "fake-service_execute" in planner.tools.list_names()
    assert verifier.tools.list_names() == ["terminate"]


@pytest.mark.parametrize("is_fake", [False, None, "true", 1])
def test_non_fake_service_with_missing_transport_fails_instead_of_using_sandbox(is_fake):
    service = {"id": "real-service", "name": "Real Service"}
    if is_fake is not None:
        service["isFake"] = is_fake
    session = _session(service)

    with pytest.raises(ServiceConnectionError, match="真实 MCP 配置无效"):
        asyncio.run(session.connect())

    assert session.statuses() == []


def test_real_mcp_connection_failure_does_not_fall_back_to_sandbox():
    class FailingConnection:
        async def connect(self, *_args, **_kwargs):
            raise RuntimeError("connection refused")

        async def disconnect_all(self):
            pass

    session = _session({
        "id": "real-service",
        "name": "Real Service",
        "isFake": False,
        "mcpMethod": "sse",
        "mcpUrl": "https://mcp.example.test/sse",
    }, connection=FailingConnection())

    with pytest.raises(ServiceConnectionError, match="MCP 连接失败"):
        asyncio.run(session.connect())

    assert session.statuses() == []
