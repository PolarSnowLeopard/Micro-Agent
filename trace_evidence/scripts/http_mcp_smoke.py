#!/usr/bin/env python3
"""HTTP 9017 单服务真 MCP 冒烟：start → stream → pipeline。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from trace_evidence import run_pipeline

BASE = "http://127.0.0.1:9017"
PAYLOAD = {
    "appId": "http-smoke",
    "appName": "【本地MCP】(1) 利奈唑胺给药 HTTP 冒烟",
    "domain": "health",
    "maxIterations": 2,
    "scenarioDescription": (
        "65岁男性院内肺炎合并肾功能减退，请调用利奈唑胺剂量计算。"
        "参数：sex=1, age=65, height=170, weight=70, scr=150, tb=20"
    ),
    "mode": "production",
    "strategy": {"minIterations": 1},
    "serviceIds": ["svc-linezolid"],
    "servicesMeta": [
        {
            "id": "svc-linezolid",
            "name": "利奈唑胺药品知识",
            "isFake": False,
            "mcpUrl": "http://127.0.0.1:25013/sse",
            "mcpMethod": "sse",
            "tools": [],
        }
    ],
}


def consume_sse(session_id: str) -> dict:
    url = f"{BASE}/api/simulation/{session_id}/stream"
    complete = None
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            event_type = None
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_type:
                    data = json.loads(line.split(":", 1)[1].strip())
                    if event_type == "complete":
                        complete = data
                        break
    return complete or {}


def main() -> int:
    with httpx.Client(timeout=60.0) as client:
        r = client.post(f"{BASE}/api/simulation/start", json=PAYLOAD)
        r.raise_for_status()
        body = r.json()
    session_id = body["sessionId"]
    print("session_id", session_id)

    complete = consume_sse(session_id)
    print("complete", json.dumps(complete, ensure_ascii=False)[:200])

    trace_path = PROJECT_ROOT / "workspace/data/traces" / f"{session_id}.json"
    for _ in range(50):
        if trace_path.exists():
            break
        import time
        time.sleep(0.2)
    if not trace_path.exists():
        print("ERROR: trace not saved", trace_path)
        return 1

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    types = [e.get("type") for e in trace.get("events", [])]
    print("trace events", len(types), "tool_call_record", types.count("tool_call_record"))
    print("metadata.trace_version", trace.get("metadata", {}).get("runtime", {}).get("trace_version"))

    result = run_pipeline(str(trace_path))
    print("checker", result.report.overall_status)
    fails = [c for c in result.report.checks if c.status in ("FAIL", "MISSING")]
    warns = [c for c in result.report.checks if c.status == "WARN"]
    print("FAIL/MISSING", len(fails), "WARN", len(warns))
    if fails:
        for c in fails[:5]:
            print(" ", c.check_name, c.status, c.detail[:80])
    return 0 if result.report.overall_status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
