"""Phase 4b 测试：MetaAppAgent + 工具 + 兼容路由。"""

import json

import pytest

from micro_agent.tool.simulated_mcp import SimulatedMCPTool
from micro_agent.tool.finalize import FinalizeResult
from micro_agent.tool.base import ToolResult
from micro_agent.core.schema import AgentEvent


# === 1. SimulatedMCPTool ===

@pytest.mark.asyncio
async def test_simulated_mcp_health():
    tool = SimulatedMCPTool(
        name="test_health",
        description="Health check",
        node_name="TestService",
        original_name="healthCheck",
    )
    result = await tool.execute()
    assert result.output
    data = json.loads(result.output)
    assert data["调用结果"] == "服务健康状态检测完成"
    assert data["服务名称"] == "TestService"


@pytest.mark.asyncio
async def test_simulated_mcp_report():
    tool = SimulatedMCPTool(
        name="gen_report",
        description="Generate report",
        node_name="ReportService",
        original_name="generateReport",
    )
    result = await tool.execute(data="test_data")
    data = json.loads(result.output)
    assert "报告" in data["调用结果"]
    assert data["输入参数"]["data"] == "test_data"


@pytest.mark.asyncio
async def test_simulated_mcp_generic():
    tool = SimulatedMCPTool(
        name="do_something",
        description="A tool",
        node_name="GenericService",
        original_name="doSomething",
    )
    result = await tool.execute()
    data = json.loads(result.output)
    assert data["调用结果"] == "工具调用完成"


# === 2. FinalizeResult ===

@pytest.mark.asyncio
async def test_finalize_result():
    tool = FinalizeResult()
    assert tool.name == "finalize_meta_result"
    result = await tool.execute(
        text_result="分析完成",
        visualization_data={"chart": "bar"},
        file_result=None,
    )
    data = json.loads(result.output)
    assert data["text_result"] == "分析完成"
    assert data["visualization_data"]["chart"] == "bar"
    assert data["file_result"] is None


# === 3. MetaAppAgent ===

def test_meta_app_agent_init():
    from micro_agent.core.config import config
    from micro_agent.core.llm import LLM
    from micro_agent.core.meta_app_agent import MetaAppAgent

    llm = LLM(config.llm)
    agent = MetaAppAgent(llm=llm)
    tool_names = agent.tools.list_names()
    assert "terminate" in tool_names
    assert "finalize_meta_result" in tool_names


@pytest.mark.asyncio
async def test_meta_app_agent_sim_init():
    from micro_agent.core.config import config
    from micro_agent.core.llm import LLM
    from micro_agent.core.meta_app_agent import MetaAppAgent

    llm = LLM(config.llm)
    agent = MetaAppAgent(llm=llm)

    meta_config = {
        "info": {
            "name": "测试元应用",
            "des": "测试用元应用",
            "inputName": "测试数据",
            "outputName": "测试结果",
        },
        "services": [
            {
                "id": "svc1",
                "name": "风险识别服务",
                "apiList": [
                    {
                        "url": "http://example.com/sse",
                        "method": "SSE",
                        "des": "风险识别 API",
                        "tools": [
                            {"id": "t1", "name": "predict", "description": "模型推理"},
                            {"id": "t2", "name": "healthCheck", "description": "健康检查"},
                        ],
                    }
                ],
            },
            {
                "id": "svc2",
                "name": "报告服务",
                "apiList": [
                    {
                        "url": "http://example.com/report/sse",
                        "method": "SSE",
                        "des": "报告生成 API",
                        "tools": [
                            {"id": "t3", "name": "generateReport", "description": "生成报告"},
                        ],
                    }
                ],
            },
        ],
    }

    await agent.initialize_from_config(meta_config, use_sim=True)
    names = agent.tools.list_names()
    assert any("predict" in n for n in names)
    assert any("healthCheck" in n for n in names)
    assert any("generateReport" in n or "Report" in n for n in names)
    assert "测试元应用" in agent.system_prompt


def test_meta_app_agent_no_nodes():
    from micro_agent.core.config import config
    from micro_agent.core.llm import LLM
    from micro_agent.core.meta_app_agent import MetaAppAgent

    llm = LLM(config.llm)
    agent = MetaAppAgent(llm=llm)

    import asyncio
    with pytest.raises(ValueError, match="未找到"):
        asyncio.run(
            agent.initialize_from_config({"info": {}, "services": []}, use_sim=True)
        )


# === 4. 模板 ===

def test_meta_app_validation_template():
    from micro_agent.task.base import render_prompt
    result = render_prompt(
        "meta_app_validation.md.j2",
        meta_app_api="http://example.com/sse",
        metrics_list=["查全率", "查准率"],
        zip_filename="test.zip",
        workspace="/work",
    )
    assert "查全率, 查准率" in result
    assert "validation_result.json" in result


def test_builtin_includes_meta_app_validation():
    import tasks.builtin  # noqa: F401
    from micro_agent.task.base import get_task
    assert get_task("meta_app_validation") is not None


# === 5. 兼容路由 ===

def test_agent_router_endpoints():
    from api.routes.agent import router
    paths = [r.path for r in router.routes]
    assert any("code_analysis" in p for p in paths)
    assert any("service_packaging" in p for p in paths)
    assert any("mcp_test" in p for p in paths)
    assert any("meta_app/run" in p for p in paths)
    assert any("capability_describe" in p for p in paths)
    assert any("capability_chat" in p for p in paths)


def test_event_to_legacy():
    from api.services.sse import event_to_legacy
    from micro_agent.core.schema import AgentEvent

    e = AgentEvent(type="think", step=1, data={"thought": "我在思考"})
    legacy = event_to_legacy(e)
    assert legacy["thought"] == "我在思考"
    assert legacy["step"] == 1

    e2 = AgentEvent(type="tool_call", step=2, data={"tool": "bash"})
    legacy2 = event_to_legacy(e2)
    assert legacy2["action"] == "bash"

    e3 = AgentEvent(type="done", step=3, data={"result": "完成"})
    legacy3 = event_to_legacy(e3)
    assert legacy3["is_last"] is True


def test_app_includes_agent_endpoints():
    from api.app import app
    paths = list(app.openapi()["paths"].keys())
    assert any("/api/agent/meta_app/run" in p for p in paths)
    assert any("/api/agent/capability_describe" in p for p in paths)
