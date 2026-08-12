"""场景想定解析 ScenarioParsed 数据结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class ScenarioSource:
    """场景证据来源：保留原始输入与追问过程。"""

    rawUserInput: str = ""
    intakeDialogue: list[dict] = field(default_factory=list)
    intakeSessionId: Optional[str] = None
    parserModel: Optional[str] = None
    parsedAt: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScenarioParsed:
    """场景想定解析的最终结论。"""

    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    acceptanceCriteria: list[str] = field(default_factory=list)
    domain: str = ""
    description: str = ""
    source: Optional[ScenarioSource] = None

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_scenario_parsed(
    raw: dict[str, Any],
    *,
    raw_user_input: str = "",
    intake_dialogue: list[dict] | None = None,
    intake_session_id: str | None = None,
    parser_model: str | None = None,
    parsed_at: str | None = None,
    domain: str = "",
) -> ScenarioParsed:
    """将 LLM 输出或 API 载荷归一化为 ScenarioParsed。"""
    if not isinstance(raw, dict):
        raw = {}

    goal = str(raw.get("goal") or "").strip()

    constraints_raw = raw.get("constraints") or []
    if not isinstance(constraints_raw, list):
        constraints_raw = []
    constraints = [str(c).strip() for c in constraints_raw if str(c).strip()]

    acceptance = raw.get("acceptanceCriteria") or []
    if not isinstance(acceptance, list):
        acceptance = []
    acceptance_criteria = [str(c).strip() for c in acceptance if str(c).strip()]

    resolved_domain = str(domain or raw.get("domain") or "").strip() or "generic"
    description = str(raw.get("description") or "").strip() or goal

    source_raw = raw.get("source")
    if isinstance(source_raw, dict):
        source = ScenarioSource(
            rawUserInput=str(source_raw.get("rawUserInput") or raw_user_input or "").strip(),
            intakeDialogue=list(source_raw.get("intakeDialogue") or intake_dialogue or []),
            intakeSessionId=source_raw.get("intakeSessionId") or intake_session_id,
            parserModel=source_raw.get("parserModel") or parser_model,
            parsedAt=source_raw.get("parsedAt") or parsed_at,
        )
    else:
        source = ScenarioSource(
            rawUserInput=raw_user_input,
            intakeDialogue=intake_dialogue or [],
            intakeSessionId=intake_session_id,
            parserModel=parser_model,
            parsedAt=parsed_at,
        )

    return ScenarioParsed(
        goal=goal,
        constraints=constraints,
        acceptanceCriteria=acceptance_criteria,
        domain=resolved_domain,
        description=description,
        source=source,
    )
