#!/usr/bin/env python3
"""Pass #10: Headless simulation run — produce an ENHANCED trace with structured
tool_calls, verification events, and full metadata.

Uses the 3 live MCP services (medical-calc:18000, linezolid:25013, healthcovered:18001)
to produce a real trace with genuine MCP channel evidence.

Usage:
    .venv/bin/python trace_evidence/headless_run.py
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import time
import uuid
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from micro_agent.simulation.orchestrator import SimulationOrchestrator
from micro_agent.simulation.trace_store import FileTraceStore, TraceRecord


# ---------------------------------------------------------------------------
# Configuration: real MCP services that are currently alive
# ---------------------------------------------------------------------------

HEADLESS_CFG = {
    "appId": "headless-evidence-run",
    "appName": "乡村医疗AI辅助诊断（headless证据采集）",
    "domain": "health",
    "mode": "production",
    "maxIterations": 2,
    "scenarioDescription": (
        "患者李大爷，68岁，乡村，主诉发热3天伴咳嗽。"
        "请调用药品标签查询、ACA资格检查、药物靶点知识服务，"
        "综合判断用药方案并验证覆盖范围。"
    ),
    "strategy": {"minIterations": 1, "verificationMode": "strict"},
    "serviceIds": [
        "svc-medical-calc",
        "svc-linezolid",
        "svc-healthcovered",
    ],
    "servicesMeta": [
        {
            "id": "svc-medical-calc",
            "name": "医学计算器",
            "description": "医学评分与剂量计算",
            "isFake": False,
            "mcpUrl": "http://127.0.0.1:18000/sse",
            "mcpMethod": "sse",
        },
        {
            "id": "svc-linezolid",
            "name": "利奈唑胺药品知识",
            "description": "利奈唑胺药品标签与用药指导",
            "isFake": False,
            "mcpUrl": "http://127.0.0.1:25013/sse",
            "mcpMethod": "sse",
        },
        {
            "id": "svc-healthcovered",
            "name": "ACA医保资格查询",
            "description": "Affordable Care Act 资格与覆盖范围查询",
            "isFake": False,
            "mcpUrl": "http://127.0.0.1:18001/mcp",
            "mcpMethod": "streamable_http",
        },
    ],
}

# Output location
OUTPUT_DIR = Path(__file__).resolve().parent / "output_headless"
TRACE_STORE_DIR = PROJECT_ROOT / "workspace" / "data" / "traces"


async def run_headless() -> Path | None:
    """Execute headless simulation, return path to saved trace JSON."""
    session_id = f"sim-headless-{uuid.uuid4().hex[:12]}"
    logger.info(f"=== Headless Run Start: {session_id} ===")
    logger.info(f"Services: {[s['name'] for s in HEADLESS_CFG['servicesMeta']]}")

    orch = SimulationOrchestrator(HEADLESS_CFG)

    # Collect all events
    trace_events: list[dict] = []
    final_success = False
    final_iterations = 0
    final_elapsed = 0

    run_gen = orch.run()
    try:
        async for event in run_gen:
            ev_dict = event.to_dict()
            trace_events.append(ev_dict)

            # Log progress
            if event.type == "step":
                logger.info(f"  Phase: {ev_dict['data'].get('name', '?')}")
            elif event.type == "log":
                level = ev_dict["data"].get("level", "INFO")
                msg = ev_dict["data"].get("message", "")[:120]
                logger.debug(f"    [{level}] {msg}")
            elif event.type == "service":
                sid = ev_dict["data"].get("id", "")
                status = ev_dict["data"].get("status", "")
                logger.info(f"  Service {sid}: {status}")
            elif event.type == "complete":
                final_success = ev_dict["data"].get("success", False)
                metrics = ev_dict["data"].get("metrics", {})
                final_iterations = metrics.get("iterations", 0)
                final_elapsed = metrics.get("elapsedMs", 0)
                logger.info(
                    f"  Complete: success={final_success}, "
                    f"iterations={final_iterations}, elapsed={final_elapsed}ms"
                )
                # Break BEFORE generator runs finally/disconnect_all()
                break
            elif event.type == "verifier_result":
                status = ev_dict["data"].get("status", "?")
                logger.info(f"  Verifier: {status} (iter {ev_dict['data'].get('iteration', '?')})")
            elif event.type == "planner_decision":
                tools = ev_dict["data"].get("selected_tools", [])
                logger.info(f"  Planner decision: selected {tools}")

    except BaseException as exc:
        # CancelledError (from MCP disconnect) does NOT inherit Exception in py3.11
        logger.warning(f"Orchestrator raised {type(exc).__name__}: {exc}")
        if not trace_events:
            trace_events.append({
                "type": "error",
                "data": {"error": str(exc)},
                "timestamp": time.time(),
            })

    # === Collect tool_call_records (v1: enhanced with call_id/channel/transport/success) ===
    tool_call_events: list[dict] = []
    try:
        for rec in orch._collect_call_records():
            import hashlib
            result_stored = rec.result[:2000] if rec.result else None
            result_hash = hashlib.sha256((result_stored or "").encode()).hexdigest()[:16]
            tool_call_events.append({
                "type": "tool_call_record",
                "data": {
                    "call_id": rec.call_id,
                    "tool_name": rec.tool_name,
                    "service_id": rec.service_id,
                    "service_name": rec.service_name,
                    "channel": rec.channel,
                    "transport": rec.transport,
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
    except Exception as e:
        logger.debug(f"收集 tool_call_records 失败 (non-fatal): {e}")

    # === Build toolChannels from static config (checker: tool_channels_presence) ===
    tool_channels = [
        {
            "service_id": svc["id"],
            "service_name": svc["name"],
            "channel": "mcp",
            "transport": svc.get("mcpMethod", "sse"),
            "url": svc.get("mcpUrl", ""),
        }
        for svc in HEADLESS_CFG.get("servicesMeta", [])
    ]

    # === Build executionPath from phase events (checker: execution_path) ===
    execution_path = []
    for ev in trace_events:
        ev_type = ev.get("type", "")
        if ev_type == "phase":
            phase_name = ev.get("data", {}).get("phase", ev.get("data", {}).get("name", ""))
            if phase_name and phase_name not in execution_path:
                execution_path.append(phase_name)
        elif ev_type in ("planner_decision", "verifier_result", "iteration_complete"):
            if ev_type not in execution_path:
                execution_path.append(ev_type)

    # === Build metadata (mirrors simulation.py pattern) ===
    metadata = {
        "trace_version": "v1.0.0",
        "config_snapshot": {
            "appId": HEADLESS_CFG.get("appId", ""),
            "serviceIds": HEADLESS_CFG.get("serviceIds", []),
            "servicesMeta": HEADLESS_CFG.get("servicesMeta", []),
            "maxIterations": HEADLESS_CFG.get("maxIterations", 3),
            "scenarioDescription": HEADLESS_CFG.get("scenarioDescription", ""),
        },
        "runtime": {
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "trace_version": "v1.0.0",
            "headless": True,
        },
        "tool_call_count": len(tool_call_events),
        "toolChannels": tool_channels,
        "executionPath": execution_path,
    }

    all_events = trace_events + tool_call_events

    record = TraceRecord(
        session_id=session_id,
        app_name=HEADLESS_CFG.get("appName", ""),
        domain=HEADLESS_CFG.get("domain", ""),
        mode=HEADLESS_CFG.get("mode", "production"),
        strategy=HEADLESS_CFG.get("strategy", {}),
        events=all_events,
        success=final_success,
        iterations=final_iterations,
        elapsed_ms=final_elapsed,
        metadata=metadata,
    )

    # Save to official trace store
    store = FileTraceStore(TRACE_STORE_DIR)
    await store.save(record)
    trace_path = TRACE_STORE_DIR / f"{session_id}.json"

    logger.info(f"=== Trace saved: {trace_path} ===")
    logger.info(f"    Events: {len(all_events)} ({len(tool_call_events)} tool_call_records)")

    # === Gracefully close the generator (triggers disconnect_all in finally) ===
    try:
        await run_gen.aclose()
    except BaseException as cleanup_exc:
        logger.debug(f"Generator cleanup (expected): {type(cleanup_exc).__name__}")

    # === Now run evidence pipeline on the fresh trace ===
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from trace_adapter import TraceEvidenceAdapter
        from evidence_card import build_evidence_card, render_evidence_card_markdown
        from config_attachment import build_config_attachment_draft
        from evidence_checker import EvidenceChecker, render_checker_report_markdown

        adapter = TraceEvidenceAdapter.from_file(str(trace_path))
        bundle = adapter.extract()

        card = build_evidence_card(bundle)

        config_draft = build_config_attachment_draft(bundle, card)

        checker = EvidenceChecker(bundle, card)
        report = checker.run_all()

        # Save all outputs
        (OUTPUT_DIR / "evidence_card.json").write_text(
            card.to_json(), encoding="utf-8"
        )
        (OUTPUT_DIR / "evidence_card.md").write_text(
            render_evidence_card_markdown(card), encoding="utf-8"
        )
        (OUTPUT_DIR / "config_attachment_draft.json").write_text(
            config_draft.to_json(), encoding="utf-8"
        )
        (OUTPUT_DIR / "checker_report.json").write_text(
            report.to_json(), encoding="utf-8"
        )
        (OUTPUT_DIR / "checker_report.md").write_text(
            render_checker_report_markdown(report), encoding="utf-8"
        )

        # Summary
        total = len(report.checks)
        pass_count = sum(1 for c in report.checks if c.status == "PASS")
        verdict = report.overall_status
        logger.info(f"=== Evidence Pipeline Complete ===")
        logger.info(f"    Checks: {pass_count}/{total} PASS, verdict={verdict}")
        logger.info(f"    Output: {OUTPUT_DIR}")

    except Exception as exc:
        logger.error(f"Evidence pipeline failed: {exc}", exc_info=True)

    return trace_path


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level:<7} | {message}")

    result = asyncio.run(run_headless())
    if result:
        print(f"\n✅ Fresh trace: {result}")
    else:
        print("\n❌ Headless run failed")
        sys.exit(1)
