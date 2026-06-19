"""Verifier 结构化 verdict 与文本回退解析单测。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from micro_agent.core.schema import AgentEvent
from micro_agent.simulation.orchestrator import SimulationOrchestrator


def _trace_with_verdict(verdict: str, summary: str) -> list[AgentEvent]:
    return [
        AgentEvent(
            type="tool_call",
            step=1,
            data={
                "tool": "terminate",
                "arguments": {"verdict": verdict, "result": summary},
            },
        ),
        AgentEvent(
            type="done",
            step=1,
            data={"tool": "terminate", "verdict": verdict, "result": summary},
        ),
    ]


def test_structured_passed():
    trace = _trace_with_verdict("passed", "【审查通过】编排完整。")
    ok, issue = SimulationOrchestrator._resolve_verification(trace, "")
    assert ok is True
    assert issue == ""


def test_structured_failed():
    trace = _trace_with_verdict("failed", "缺少 SOFA 评分调用")
    ok, issue = SimulationOrchestrator._resolve_verification(trace, "")
    assert ok is False
    assert "SOFA" in issue


def test_structured_overrides_misleading_text():
    trace = _trace_with_verdict("passed", "【审查通过】肾功能减退肺炎患者服务编排执行轨迹审查完毕。")
    ok, issue = SimulationOrchestrator._resolve_verification(trace, "FAILED: should not win")
    assert ok is True
    assert issue == ""


def test_fallback_parse_when_no_verdict():
    ok, issue = SimulationOrchestrator._resolve_verification([], "FAILED: openFDA 无结果")
    assert ok is False
    assert "openFDA" in issue


def test_parse_verification_legacy_passed():
    ok, issue = SimulationOrchestrator._parse_verification("【审查结论】PASSED")
    assert ok is True
    assert issue == ""


def test_parse_verification_empty():
    ok, issue = SimulationOrchestrator._parse_verification("")
    assert ok is False
    assert issue == "验证 Agent 未产生有效结论"
