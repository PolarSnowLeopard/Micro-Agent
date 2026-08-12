"""Stable scenario-intake contracts kept in the regular CI suite."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_parse_intake_json():
    from micro_agent.scenario.scenario_intake import _parse_intake_json

    assert _parse_intake_json('{"status":"question","text":"目标是什么？"}')["status"] == "question"
    wrapped = '```json\n{"status":"ready","scenarioParsed":{"goal":"x"}}\n```'
    assert _parse_intake_json(wrapped)["status"] == "ready"
    assert _parse_intake_json("") is None


def test_build_intake_system_uses_requested_domain():
    from micro_agent.scenario.scenario_intake import _build_intake_system

    system = _build_intake_system("aml")

    assert '"domain":"aml"' in system
    assert '当前请求领域代码为 "aml"' in system


def test_normalize_scenario_parsed():
    from micro_agent.scenario.schema import normalize_scenario_parsed

    scenario = normalize_scenario_parsed(
        {
            "goal": "g",
            "description": "院内场景",
            "constraints": ["c"],
            "acceptanceCriteria": ["验收"],
            "domain": "health",
        },
        raw_user_input="原始输入",
    )
    assert scenario.goal == "g"
    assert scenario.description == "院内场景"
    assert scenario.constraints == ["c"]
    assert scenario.acceptanceCriteria == ["验收"]
    assert scenario.domain == "health"
    assert scenario.source.rawUserInput == "原始输入"
    assert "ioExpectation" not in scenario.to_dict()
    assert "situationBrief" not in scenario.to_dict()


def test_normalize_scenario_parsed_prefers_requested_domain():
    from micro_agent.scenario.schema import normalize_scenario_parsed

    scenario = normalize_scenario_parsed(
        {"goal": "跨境支付风险识别", "domain": "generic"},
        domain="aml",
    )

    assert scenario.domain == "aml"


async def test_run_scenario_intake_question():
    from micro_agent.scenario.scenario_intake import run_scenario_intake_turn

    response = MagicMock()
    response.content = json.dumps({"status": "question", "text": "期望输出是什么？"}, ensure_ascii=False)
    memory = MagicMock()
    memory.load = AsyncMock()
    memory.to_list.return_value = [{"role": "user", "content": "做用药方案"}]
    memory.persist = AsyncMock()

    with patch("micro_agent.scenario.scenario_intake.LLM") as llm_class, patch(
        "micro_agent.scenario.scenario_intake.FileMemory", return_value=memory
    ):
        llm_class.return_value.complete = AsyncMock(return_value=response)
        llm_class.return_value.model = "test-model"
        result = await run_scenario_intake_turn(message="做用药方案", domain="health")

    assert result["status"] == "question"
    assert "输出" in result["text"]
    assert result["session_id"]


@pytest.mark.parametrize("domain", ["", "generic", "not-configured"])
async def test_run_scenario_intake_rejects_invalid_business_domain(domain):
    from micro_agent.scenario.scenario_intake import (
        ScenarioDomainError,
        run_scenario_intake_turn,
    )

    with patch("micro_agent.scenario.scenario_intake.LLM") as llm_class:
        with pytest.raises(ScenarioDomainError):
            await run_scenario_intake_turn(message="构建风险识别应用", domain=domain)

    llm_class.assert_not_called()


async def test_run_scenario_intake_ready():
    from micro_agent.scenario.scenario_intake import run_scenario_intake_turn

    response = MagicMock()
    response.content = json.dumps(
        {
            "status": "ready",
            "text": "信息已足够",
            "userRemark": "院内肺炎用药",
            "scenarioSummary": "65岁男性院内肺炎，需利奈唑胺方案",
            "scenarioParsed": {
                "goal": "制定给药方案",
                "description": "65岁男性院内肺炎",
                "constraints": ["肾功能不全"],
                "acceptanceCriteria": ["给出剂量"],
                "domain": "health",
            },
        },
        ensure_ascii=False,
    )
    memory = MagicMock()
    memory.load = AsyncMock()
    memory.to_list.return_value = [{"role": "user", "content": "完整描述"}]
    memory.persist = AsyncMock()

    with patch("micro_agent.scenario.scenario_intake.LLM") as llm_class, patch(
        "micro_agent.scenario.scenario_intake.FileMemory", return_value=memory
    ):
        llm_class.return_value.complete = AsyncMock(return_value=response)
        llm_class.return_value.model = "test-model"
        result = await run_scenario_intake_turn(message="完整描述", domain="health")

    assert result["status"] == "ready"
    assert result["scenarioParsed"]["goal"] == "制定给药方案"
    assert result["scenarioParsed"]["description"] == "65岁男性院内肺炎"
    assert result["scenarioParsed"]["domain"] == "health"
    assert result["userRemark"] == "院内肺炎用药"


async def test_complete_aml_contract_corrects_redundant_question():
    from micro_agent.scenario.scenario_intake import run_scenario_intake_turn

    question_response = MagicMock()
    question_response.content = json.dumps(
        {"status": "question", "text": "请再明确业务目标和期望输出。"},
        ensure_ascii=False,
    )
    ready_response = MagicMock()
    ready_response.content = json.dumps(
        {
            "status": "ready",
            "text": "场景已清晰",
            "scenarioSummary": "跨境支付可疑交易风险识别与审计",
            "scenarioParsed": {
                "goal": "识别跨境支付高风险交易",
                "description": "综合交易、KYC、制裁与拒付数据进行风险识别",
                "constraints": ["输出可审计"],
                "acceptanceCriteria": ["识别高风险交易并生成审计报告"],
                "domain": "generic",
            },
        },
        ensure_ascii=False,
    )
    user_input = (
        "我要构建跨境支付可疑交易风险识别元应用。输入是交易流水、KYC、制裁名单和拒付记录；"
        "输出是风险等级、可疑原因、处置建议和审计报告；验收标准是能识别高风险交易并生成可审计报告。"
    )
    memory = MagicMock()
    memory.load = AsyncMock()
    memory.to_list.return_value = [{"role": "user", "content": user_input}]
    memory.persist = AsyncMock()

    with patch("micro_agent.scenario.scenario_intake.LLM") as llm_class, patch(
        "micro_agent.scenario.scenario_intake.FileMemory", return_value=memory
    ):
        llm_class.return_value.complete = AsyncMock(
            side_effect=[question_response, ready_response]
        )
        llm_class.return_value.model = "test-model"
        result = await run_scenario_intake_turn(message=user_input, domain="aml")

    assert llm_class.return_value.complete.await_count == 2
    assert result["status"] == "ready"
    assert result["scenarioParsed"]["domain"] == "aml"


async def test_invalid_intake_json_is_repaired_without_user_reprompt():
    from micro_agent.scenario.scenario_intake import run_scenario_intake_turn

    invalid_response = MagicMock()
    invalid_response.content = "我需要再想一下"
    ready_response = MagicMock()
    ready_response.content = json.dumps(
        {
            "status": "ready",
            "text": "信息已足够",
            "scenarioSummary": "风险识别场景",
            "scenarioParsed": {"goal": "风险识别", "domain": "aml"},
        },
        ensure_ascii=False,
    )
    memory = MagicMock()
    memory.load = AsyncMock()
    memory.to_list.return_value = [{"role": "user", "content": "构建风险识别应用"}]
    memory.persist = AsyncMock()

    with patch("micro_agent.scenario.scenario_intake.LLM") as llm_class, patch(
        "micro_agent.scenario.scenario_intake.FileMemory", return_value=memory
    ):
        llm_class.return_value.complete = AsyncMock(
            side_effect=[invalid_response, ready_response]
        )
        llm_class.return_value.model = "test-model"
        result = await run_scenario_intake_turn(message="构建风险识别应用", domain="aml")

    assert result["status"] == "ready"
    assert result["text"] != "能再具体说明一下你的业务目标和期望输出吗？"


async def test_orchestrator_reuses_scenario_parsed():
    from micro_agent.simulation.orchestrator import SimulationOrchestrator

    orchestrator = SimulationOrchestrator(
        {
            "scenarioParsed": {
                "goal": "测试目标",
                "description": "测试场景",
                "constraints": [],
                "acceptanceCriteria": [],
                "domain": "generic",
            },
            "scenarioDescription": "",
        }
    )
    parsed = await orchestrator._parse_scenario_parsed()
    assert parsed["goal"] == "测试目标"
    assert parsed["description"] == "测试场景"
