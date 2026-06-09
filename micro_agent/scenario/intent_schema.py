"""parsedIntent 结构归一化（对话追问与仿真解析共用）。"""

from __future__ import annotations

from typing import Any


def normalize_parsed_intent(intent: Any) -> dict[str, Any]:
    if not isinstance(intent, dict):
        intent = {}
    io = intent.get("ioExpectation")
    if not isinstance(io, dict):
        io = {}
    acceptance = intent.get("acceptanceCriteria")
    if acceptance is None:
        acceptance = intent.get("successCriteria")
    return {
        "goal": str(intent.get("goal") or "").strip(),
        "situationBrief": str(intent.get("situationBrief") or "").strip(),
        "constraints": [str(c).strip() for c in (intent.get("constraints") or []) if str(c).strip()],
        "acceptanceCriteria": [str(c).strip() for c in (acceptance or []) if str(c).strip()],
        "ioExpectation": {
            "inputs": [str(x).strip() for x in (io.get("inputs") or []) if str(x).strip()],
            "outputs": [str(x).strip() for x in (io.get("outputs") or []) if str(x).strip()],
        },
    }
