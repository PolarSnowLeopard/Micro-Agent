"""Stable scenario-intake contracts kept in the regular CI suite."""

import json
from unittest.mock import AsyncMock, MagicMock, patch


def test_parse_intake_json():
    from micro_agent.scenario.scenario_intake import _parse_intake_json

    assert _parse_intake_json('{"status":"question","text":"目标是什么？"}')["status"] == "question"
    wrapped = '```json\n{"status":"ready","scenarioParsed":{"goal":"x"}}\n```'
    assert _parse_intake_json(wrapped)["status"] == "ready"
    assert _parse_intake_json("") is None


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
        result = await run_scenario_intake_turn(message="做用药方案", domain="generic")

    assert result["status"] == "question"
    assert "输出" in result["text"]
    assert result["session_id"]


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
