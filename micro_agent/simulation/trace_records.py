"""仿真轨迹落盘：tool_call_record 事件与 metadata（v1.0.0）。"""

from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCallRecord:
    tool_name: str
    service_id: str
    arguments: dict
    result: str
    error: str | None
    latency_ms: int
    timestamp: float
    call_id: str = ""
    service_name: str = ""
    channel: str = "unknown"
    transport: str = "unknown"
    success: bool = True
    source: str = ""
    phase: str = ""
    purpose: str = ""
    iteration: int | None = None
    react_step_id: str = ""
    action_id: str = ""


def annotate_records(records: list[ToolCallRecord], phase: str, purpose: str) -> None:
    for record in records:
        record.phase = record.phase or phase
        record.purpose = record.purpose or purpose
        record.source = record.source or record.channel


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
            "servicesMeta": cfg.get("servicesMeta", []),
            "maxIterations": cfg.get("maxIterations", 5),
            "scenarioDescription": cfg.get("scenarioDescription", ""),
            "scenarioParsed": cfg.get("scenarioParsed", {}),
        },
        "runtime": runtime,
        "tool_call_count": tool_call_count,
    }
