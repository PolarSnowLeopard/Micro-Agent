"""Phase 4 测试：Task 配置系统 + Prompt 模板 + Agent 路由。"""

import asyncio

import pytest

from micro_agent.task.base import TaskConfig, render_prompt, register_task, get_task, list_tasks
from micro_agent.core.schema import AgentEvent


# === 1. TaskConfig 基础功能 ===

def test_task_config_creation():
    config = TaskConfig(
        name="test_task",
        description="测试任务",
        prompt_template="inline: {{ name }}",
        system_prompt="你是测试 Agent",
    )
    assert config.name == "test_task"
    assert config.max_steps == 30
    assert config.use_mcp is True


def test_render_inline_template():
    result = render_prompt("Hello {{ name }}, you have {{ count }} tasks.", name="梵宇", count=3)
    assert result == "Hello 梵宇, you have 3 tasks."


def test_render_file_template():
    result = render_prompt(
        "code_analysis.md.j2",
        workspace="/tmp/workspace",
        input_dir="input",
        main_code="main.py",
        temp_dir="temp",
        function_info_path="function.json",
    )
    assert "/tmp/workspace" in result
    assert "main.py" in result
    assert "function.json" in result
    assert "nodes" in result


def test_render_mcp_test_template():
    result = render_prompt(
        "mcp_test.md.j2",
        message="请测试 MySQL 服务",
        workspace="/data",
    )
    assert "请测试 MySQL 服务" in result
    assert "/data/temp/mcp_server_list.md" in result


def test_render_service_packaging_template():
    result = render_prompt(
        "service_packaging.md.j2",
        workspace="/work",
        input_dir="input",
        main_code="main.py",
        output_dir="output",
        temp_dir="temp",
        function_info_path="function.json",
    )
    assert "MCP" in result
    assert "Dockerfile" in result
    assert "/work" in result


def test_render_service_evaluation_template():
    result = render_prompt(
        "service_evaluation.md.j2",
        service_name="风险识别服务",
        metrics_list=["准确率", "响应时间"],
        zip_filename="data.zip",
        base_url="https://fdueblab.cn",
        service_info="{}",
        workspace="/work",
    )
    assert "风险识别服务" in result
    assert "准确率, 响应时间" in result


def test_render_mcp_recommendation_template():
    result = render_prompt(
        "mcp_service_recommendation.md.j2",
        message="我需要一个报告生成应用",
        service_type="aml",
        workspace="/work",
    )
    assert "报告生成应用" in result
    assert "atomic_mcp" in result
    assert "serviceIds" in result
    assert "tool_name" in result
    assert "tool_description" in result
    assert "pre_release_unrated" in result
    assert "pre_release_pending" in result
    assert "released" in result
    assert "nodeList" not in result
    assert '"success": false' in result
    assert '"result"' in result


# === 2. 任务注册表 ===

def test_register_and_get():
    register_task(TaskConfig(name="test_reg_1", description="test"))
    config = get_task("test_reg_1")
    assert config is not None
    assert config.name == "test_reg_1"


def test_get_nonexistent():
    assert get_task("nonexistent_task_xyz") is None


def test_builtin_tasks_registered():
    import tasks.builtin  # noqa: F401
    names = list_tasks()
    assert "code_analysis" in names
    assert "service_packaging" in names
    assert "mcp_test" in names
    assert "service_evaluation" in names
    assert "mcp_service_recommendation" in names


# === 3. Agent 路由 import ===

def test_agent_router_import():
    from api.routes.agent import router
    routes = [r.path for r in router.routes]
    assert "/tasks" in routes or any("task" in r for r in routes)


def test_app_includes_agent_router():
    from api.app import app
    paths = list(app.openapi()["paths"].keys())
    assert any("/api/agent" in p for p in paths)
