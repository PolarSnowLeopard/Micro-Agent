"""仿真轨迹落盘：tool_call_record 事件与 metadata（v1.0.0）。"""

from __future__ import annotations

import hashlib
import platform
from typing import Any

from micro_agent.simulation.sandbox_tool import ToolCallRecord


def build_tool_call_record_events(records: list[ToolCallRecord]) -> list[dict]:
    events: list[dict] = []
    for rec in records:
        result_stored = rec.result[:2000] if rec.result else None
        result_hash = hashlib.sha256((result_stored or "").encode()).hexdigest()[:16]
        events.append({
            "type": "tool_call_record",
            "data": {
                "call_id": rec.call_id,
                "tool_name": rec.tool_name,
                "service_id": rec.service_id,
                "service_name": rec.service_name,
                "channel": rec.channel,
                "transport": rec.transport,
                "source": rec.source or rec.channel,
                "phase": rec.phase,
                "purpose": rec.purpose,
                "iteration": rec.iteration,
                "react_step_id": rec.react_step_id,
                "action_id": rec.action_id,
                "arguments": rec.arguments,
                "result": result_stored,
                "result_hash": result_hash,
                "error": rec.error,
                "latency_ms": rec.latency_ms,
                "timestamp": rec.timestamp,
                "success": rec.success,
            },
            "timestamp": rec.timestamp,
        })
    return events


def build_trace_metadata(cfg: dict[str, Any], tool_call_count: int, *, headless: bool = False) -> dict:
    runtime: dict[str, Any] = {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "trace_version": "v1.0.0",
    }
    if headless:
        runtime["headless"] = True
    return {
        "trace_version": "v1.0.0",
        "config_snapshot": {
            "appId": cfg.get("appId", ""),
            "appName": cfg.get("appName", ""),
            "domain": cfg.get("domain", ""),
            "serviceIds": cfg.get("serviceIds", []),
            "servicesMeta": cfg.get("servicesMeta", []),
            "maxIterations": cfg.get("maxIterations", 5),
            "scenarioDescription": cfg.get("scenarioDescription", ""),
            "scenarioSummary": cfg.get("scenarioSummary", ""),
            "scenarioParsed": cfg.get("scenarioParsed", {}),
        },
        "runtime": runtime,
        "tool_call_count": tool_call_count,
    }
