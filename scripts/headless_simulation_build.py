#!/usr/bin/env python3
"""Headless 全链路仿真构建。

读取 external-mcp/service_catalog.json 中预解析好的真实场景与服务集，
对每个场景跑 SimulationOrchestrator，并通过 BuildBundleStore 落地一份
BuildBundle（trace + MetaAppArtifact v1）。不走追问/推荐，不入库、不验证，
但对每个场景断言「最小可用」：

  - 仿真 complete 且 success=True
  - artifact.goldenPaths 非空，且 runtime.serviceBindings 非空
  - trace 至少包含一条 source=real_mcp 的 tool_call_record

用法：
    .venv/bin/python scripts/headless_simulation_build.py
    .venv/bin/python scripts/headless_simulation_build.py sepsis_bedside pe_risk
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from micro_agent.simulation.build_bundle import BuildBundleStore
from micro_agent.simulation.orchestrator import SimulationOrchestrator
from micro_agent.simulation.trace_records import (
    build_tool_call_record_events,
    build_trace_metadata,
)

CATALOG_PATH = (
    PROJECT_ROOT.parent / "external-mcp" / "service_catalog.json"
)


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def build_config(catalog: dict, scenario: dict) -> dict:
    """把 catalog 中的场景 + 服务元数据拼成 orchestrator 配置。"""
    by_id = {s["serviceId"]: s for s in catalog["services"]}
    services_meta = []
    for sid in scenario["services"]:
        svc = by_id[sid]
        services_meta.append({
            "id": svc["serviceId"],
            "name": svc["name"],
            "description": svc["des"],
            "isFake": False,
            "mcpUrl": svc["localUrl"],
            "mcpMethod": svc.get("mcpMethod", "sse"),
        })
    return {
        "appId": f"headless-{scenario['key']}",
        "appName": scenario["appName"],
        "domain": catalog.get("domain", "health"),
        "mode": "production",
        "maxIterations": 2,
        "scenarioDescription": scenario["scenarioDescription"],
        "scenarioSummary": scenario["scenarioDescription"],
        "strategy": {"minIterations": 1, "verificationMode": "strict"},
        "serviceIds": [s["serviceId"] for s in (by_id[i] for i in scenario["services"])],
        "servicesMeta": services_meta,
    }


async def run_one(scenario: dict, cfg: dict, store: BuildBundleStore) -> dict:
    """跑单个场景，落地 BuildBundle，返回该场景的验收结果。"""
    build_id = f"build-headless-{scenario['key']}-{uuid.uuid4().hex[:8]}"
    logger.info(f"=== 场景 [{scenario['key']}] {scenario['title']} → {build_id} ===")
    logger.info(f"    服务：{[s['name'] for s in cfg['servicesMeta']]}")

    orch = SimulationOrchestrator(cfg)
    trace_events: list[dict] = []
    final_success = False
    final_iterations = 0
    final_elapsed = 0

    run_gen = orch.run()
    try:
        async for event in run_gen:
            ev = event.to_dict()
            trace_events.append(ev)
            if event.type == "step":
                logger.info(f"    Phase: {ev['data'].get('name', '?')}")
            elif event.type == "complete":
                final_success = bool(ev["data"].get("success"))
                metrics = ev["data"].get("metrics") or {}
                final_iterations = int(metrics.get("iterations") or 0)
                final_elapsed = int(metrics.get("elapsedMs") or 0)
                break
    except BaseException as exc:  # noqa: BLE001 - CancelledError 不继承 Exception
        logger.warning(f"orchestrator 抛出 {type(exc).__name__}: {exc}")

    try:
        tool_events = build_tool_call_record_events(orch._collect_call_records())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"收集 tool_call_records 失败（非致命）: {exc}")
        tool_events = []

    trace = {
        "schemaVersion": "build_trace.v1",
        "build_id": build_id,
        "session_id": build_id,
        "app_name": cfg["appName"],
        "domain": cfg["domain"],
        "mode": cfg["mode"],
        "strategy": cfg["strategy"],
        "events": trace_events + tool_events,
        "success": final_success,
        "iterations": final_iterations,
        "elapsed_ms": final_elapsed,
        "metadata": build_trace_metadata(cfg, len(tool_events), headless=True),
    }

    try:
        await run_gen.aclose()
    except BaseException as exc:  # noqa: BLE001
        logger.debug(f"生成器清理（预期）: {type(exc).__name__}")

    manifest = store.save_from_trace(trace)
    bundle_dir = store.bundle_dir(build_id)
    artifact = store.load_part(build_id, "artifact") or {}

    golden_paths = artifact.get("goldenPaths") or []
    bindings = (artifact.get("runtime") or {}).get("serviceBindings") or []
    real_calls = [
        e.get("data") for e in trace["events"]
        if e.get("type") == "tool_call_record"
        and (e.get("data") or {}).get("source") == "real_mcp"
    ]

    checks = {
        "success": final_success,
        "goldenPaths_non_empty": bool(golden_paths),
        "serviceBindings_non_empty": bool(bindings),
        "real_mcp_tool_call": len(real_calls) > 0,
        "researchEligible": bool(manifest.get("researchEligible")),
    }
    minimal_viable = all([
        checks["success"],
        checks["goldenPaths_non_empty"],
        checks["serviceBindings_non_empty"],
        checks["real_mcp_tool_call"],
    ])

    logger.info(f"    产物：{bundle_dir / 'artifact.json'}")
    logger.info(f"    校验：{checks} → 最小可用={minimal_viable}")

    return {
        "scenario": scenario["key"],
        "buildId": build_id,
        "bundleDir": str(bundle_dir),
        "checks": checks,
        "minimalViable": minimal_viable,
        "elapsedMs": final_elapsed,
        "realCalls": len(real_calls),
    }


async def run_headless(selected: list[str] | None = None) -> int:
    catalog = load_catalog()
    scenarios = catalog["scenarios"]
    if selected:
        scenarios = [s for s in scenarios if s["key"] in selected]
        if not scenarios:
            logger.error(f"没有匹配的场景：{selected}")
            return 1

    store = BuildBundleStore()
    results = []
    started = time.time()
    for scenario in scenarios:
        cfg = build_config(catalog, scenario)
        results.append(await run_one(scenario, cfg, store))

    logger.info("=" * 60)
    logger.info("Headless 汇总：")
    ok = 0
    for r in results:
        flag = "PASS" if r["minimalViable"] else "FAIL"
        if r["minimalViable"]:
            ok += 1
        logger.info(
            f"  [{flag}] {r['scenario']:<16} realCalls={r['realCalls']} "
            f"elapsed={r['elapsedMs']}ms build={r['buildId']}"
        )
    logger.info(f"  {ok}/{len(results)} 场景达到最小可用，总耗时 {int((time.time()-started)*1000)}ms")
    logger.info("=" * 60)

    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    code = asyncio.run(run_headless(args or None))
    sys.exit(code)
