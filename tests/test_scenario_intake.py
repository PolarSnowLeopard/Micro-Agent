"""想定追问（scenario_intake）单元测试。"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_parse_intake_json():
    from micro_agent.scenario.scenario_intake import _parse_intake_json

    assert _parse_intake_json('{"status":"question","text":"目标是什么？"}')["status"] == "question"
    wrapped = '```json\n{"status":"ready","scenarioParsed":{"goal":"x"}}\n```'
    assert _parse_intake_json(wrapped)["status"] == "ready"
    assert _parse_intake_json("") is None


def test_normalize_scenario_parsed():
    from micro_agent.scenario.schema import normalize_scenario_parsed

    sp = normalize_scenario_parsed(
        {
            "goal": "g",
            "description": "院内场景",
            "constraints": ["c"],
            "acceptanceCriteria": ["验收"],
            "domain": "health",
        },
        raw_user_input="原始输入",
    )
    assert sp.goal == "g"
    assert sp.description == "院内场景"
    assert sp.constraints == ["c"]
    assert sp.acceptanceCriteria == ["验收"]
    assert sp.domain == "health"
    assert sp.source.rawUserInput == "原始输入"
    d = sp.to_dict()
    assert "ioExpectation" not in d
    assert "situationBrief" not in d


async def test_run_scenario_intake_question():
    from micro_agent.scenario.scenario_intake import run_scenario_intake_turn

    mock_resp = MagicMock()
    mock_resp.content = json.dumps({"status": "question", "text": "期望输出是什么？"}, ensure_ascii=False)

    mem = MagicMock()
    mem.load = AsyncMock()
    mem.to_list.return_value = [{"role": "user", "content": "做用药方案"}]
    mem.persist = AsyncMock()

    with patch("micro_agent.scenario.scenario_intake.LLM") as LLMCls, patch(
        "micro_agent.scenario.scenario_intake.FileMemory", return_value=mem
    ):
        LLMCls.return_value.complete = AsyncMock(return_value=mock_resp)
        LLMCls.return_value.model = "test-model"

        result = await run_scenario_intake_turn(message="做用药方案", domain="generic")

    assert result["status"] == "question"
    assert "输出" in result["text"]
    assert result["session_id"]


async def test_run_scenario_intake_ready():
    from micro_agent.scenario.scenario_intake import run_scenario_intake_turn

    ready = {
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
    }
    mock_resp = MagicMock()
    mock_resp.content = json.dumps(ready, ensure_ascii=False)

    mem = MagicMock()
    mem.load = AsyncMock()
    mem.to_list.return_value = [{"role": "user", "content": "完整描述"}]
    mem.persist = AsyncMock()

    with patch("micro_agent.scenario.scenario_intake.LLM") as LLMCls, patch(
        "micro_agent.scenario.scenario_intake.FileMemory", return_value=mem
    ):
        LLMCls.return_value.complete = AsyncMock(return_value=mock_resp)
        LLMCls.return_value.model = "test-model"

        result = await run_scenario_intake_turn(message="完整描述", domain="health")

    assert result["status"] == "ready"
    assert result["scenarioParsed"]["goal"] == "制定给药方案"
    assert result["scenarioParsed"]["description"] == "65岁男性院内肺炎"
    assert result["scenarioParsed"]["domain"] == "health"
    assert result["userRemark"] == "院内肺炎用药"


async def test_orchestrator_reuses_scenario_parsed():
    from micro_agent.simulation.orchestrator import SimulationOrchestrator

    pre = {
        "goal": "测试目标",
        "description": "测试场景",
        "constraints": [],
        "acceptanceCriteria": [],
        "domain": "generic",
    }
    orch = SimulationOrchestrator({"scenarioParsed": pre, "scenarioDescription": ""})
    parsed = await orch._parse_scenario_parsed()
    assert parsed["goal"] == "测试目标"
    assert parsed["description"] == "测试场景"


def main():
    test_parse_intake_json()
    test_normalize_scenario_parsed()
    asyncio.run(test_run_scenario_intake_question())
    asyncio.run(test_run_scenario_intake_ready())
    asyncio.run(test_orchestrator_reuses_scenario_parsed())
    print("[PASS] scenario_intake tests")


if __name__ == "__main__":
    main()
