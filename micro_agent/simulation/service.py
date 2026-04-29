"""仿真构建会话管理与 SSE 事件序列。

当前阶段（Week 1）：占位流水线，输出与 ioeb 前端 `simulation_builder.js`
订阅的 SSE 具名事件格式完全一致（见 ioeb design_docs/build-design4llm.md §7）。

后续演进：将占位逻辑替换为 SimulationAgent（Planner + Verifier + CoW 沙箱）。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

DEFAULT_STRATEGY: Dict[str, str] = {
    "sandbox": "cow",
    "planning": "llm_autonomous",
    "verification": "multi_agent",
    "repair": "llm_repair",
    "solidify": "golden_trace",
}

ENV_TASKS = ["初始化沙箱环境", "配置写操作拦截", "加载拟真数据集"]
GEN_TASKS = ["存储执行方案", "生成配置文件", "保存验证报告"]

DELAYS: Dict[str, tuple[int, int]] = {
    "serviceCheck": (350, 600),
    "envItem": (500, 700),
    "phase": (550, 900),
    "genItem": (450, 650),
    "metricsTick": (80, 80),
}

METRIC_RANGES = {
    "sandboxFidelity": (0.78, 0.96),
    "verificationAccuracy": (0.82, 0.97),
    "repairEffectiveness": (0.75, 0.95),
}


def _rand(lo: float, hi: float) -> float:
    return lo + random.random() * (hi - lo)


async def _delay(key: str) -> None:
    lo, hi = DELAYS[key]
    await asyncio.sleep(_rand(lo, hi) / 1000.0)


def _merge_strategy(body: Dict[str, Any]) -> Dict[str, str]:
    s = dict(DEFAULT_STRATEGY)
    s.update(body.get("strategy") or {})
    return s


def _module_metrics(iteration: int, elapsed_ms: int) -> Dict[str, Any]:
    sf = _rand(*METRIC_RANGES["sandboxFidelity"])
    va = _rand(*METRIC_RANGES["verificationAccuracy"])
    re_ = _rand(*METRIC_RANGES["repairEffectiveness"]) if iteration > 1 else 1.0
    planning = 1.0 if iteration == 1 else 0.4 + 0.5 / iteration
    return {
        "iterations": iteration,
        "elapsedMs": elapsed_ms,
        "sandboxFidelity": sf,
        "planningAccuracy": planning,
        "verificationAccuracy": va,
        "repairEffectiveness": re_,
    }


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

@dataclass
class SimulationSession:
    id: str
    body: Dict[str, Any]
    strategy: Dict[str, str]
    mode: str
    cancelled: bool = False
    result: Optional[Dict[str, Any]] = None
    started_at: float = field(default_factory=time.time)


_sessions: Dict[str, SimulationSession] = {}
_records: List[Dict[str, Any]] = []
_id_seq = 0


def _gen_id() -> str:
    global _id_seq
    _id_seq += 1
    return f"sim-{int(time.time() * 1000)}-{_id_seq}"


class Cancelled(Exception):
    pass


def start_session(body: Dict[str, Any]) -> Dict[str, Any]:
    sid = _gen_id()
    session = SimulationSession(
        id=sid,
        body=body,
        strategy=_merge_strategy(body),
        mode=body.get("mode") or "production",
    )
    _sessions[sid] = session
    return {
        "success": True,
        "sessionId": sid,
        "streamUrl": f"/api/simulation/{sid}/stream",
    }


def cancel_session(sid: str) -> None:
    s = _sessions.get(sid)
    if s:
        s.cancelled = True


def get_session(sid: str) -> Optional[SimulationSession]:
    return _sessions.get(sid)


def get_result(sid: str) -> Dict[str, Any]:
    s = _sessions.get(sid)
    if not s:
        return {"success": False, "error": "session_not_found"}
    if s.result is None:
        return {"success": False, "pending": True}
    return s.result


def list_records() -> List[Dict[str, Any]]:
    return list(_records)


def compare_records(record_ids: List[str]) -> Dict[str, Any]:
    by_id = {r["recordId"]: r for r in _records}
    out = []
    for rid in record_ids:
        r = by_id.get(rid)
        if r:
            out.append({
                "recordId": r["recordId"],
                "strategy": r["strategy"],
                "metrics": r["metrics"],
                "createdAt": r["createdAt"],
            })
    return {"records": out}


def _push_record(session: SimulationSession, metrics: Dict[str, Any], success: bool) -> None:
    if session.mode != "research":
        return
    _records.insert(0, {
        "recordId": f"rec-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
        "appName": session.body.get("appName", ""),
        "strategy": dict(session.strategy),
        "metrics": dict(metrics),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "success": success,
    })


# ---------------------------------------------------------------------------
# SSE event helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: Dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# SSE stream generator (placeholder pipeline, async)
# ---------------------------------------------------------------------------

async def iter_sse(sid: str) -> AsyncIterator[str]:
    session = get_session(sid)
    if not session:
        yield _sse("complete", {"success": False, "result": {"error": "无效会话"}})
        return

    body = session.body
    strategy = session.strategy
    services_meta = body.get("servicesMeta") or []
    is_research = session.mode == "research"

    def _check() -> None:
        s = get_session(sid)
        if s and s.cancelled:
            raise Cancelled()

    def _log(level: str, message: str) -> str:
        _check()
        return _sse("log", {"level": level, "message": message})

    try:
        # Step 0: 服务匹配
        yield _sse("step", {"step": 0, "name": "服务匹配"})
        yield _log("INFO", "开始服务匹配")
        yield _log("INFO", "（占位）领域知识增强已跳过")

        for svc in services_meta:
            _check()
            await _delay("serviceCheck")
            sid_svc = svc.get("id", "")
            name = svc.get("name", sid_svc)
            yield _log("INFO", f"检测服务: {name}")
            latency = 120
            yield _sse("service", {"id": sid_svc, "status": "online", "latency": latency})
            yield _log("SUCCESS", f"{name} 连接正常 ({latency}ms)")
        yield _log("SUCCESS", "服务匹配完成")

        # Step 1: 环境准备
        _check()
        yield _sse("step", {"step": 1, "name": "环境准备"})
        yield _log("INFO", "开始准备仿真环境")
        for i, text in enumerate(ENV_TASKS):
            _check()
            yield _sse("progress", {"ctx": "env", "index": i, "text": text, "active": True})
            await _delay("envItem")
            yield _sse("progress", {"ctx": "env", "index": i, "text": text, "done": True})
            yield _log("INFO", text)
        yield _log("SUCCESS", "环境准备完成")

        # Step 2: 智能构建
        yield _sse("step", {"step": 2, "name": "智能构建"})
        yield _log("INFO", "开始智能构建")
        iteration = 1
        _check()
        yield _sse("iteration", {"iteration": iteration, "status": "running"})
        yield _log("INFO", f"开始第 {iteration} 轮验证")

        for phase in ("data", "logic", "check"):
            _check()
            yield _sse("phase", {"phase": phase, "status": "running"})
            await _delay("phase")
            yield _sse("phase", {"phase": phase, "status": "done"})

        yield _log("SUCCESS", "数据仿真: 数据流转正常")
        yield _log("SUCCESS", "逻辑仿真: 业务逻辑正常")
        yield _log("INFO", "链路检视: 检查偏差和冗余")
        yield _log("SUCCESS", "链路检视: 未发现偏差")
        yield _sse("iteration", {"iteration": iteration, "status": "passed"})

        elapsed_ms = int((time.time() - session.started_at) * 1000)
        metrics = _module_metrics(iteration, elapsed_ms)

        if is_research:
            tick = _rand(*DELAYS["metricsTick"]) / 1000.0
            for mod, key in (
                ("sandbox", "sandboxFidelity"),
                ("planning", "planningAccuracy"),
                ("verification", "verificationAccuracy"),
                ("repair", "repairEffectiveness"),
            ):
                yield _sse("metrics", {"module": mod, "metric": key, "value": metrics[key]})
                await asyncio.sleep(tick)

        # Step 3: 方案生成
        _check()
        yield _sse("step", {"step": 3, "name": "方案生成"})
        yield _log("INFO", "开始生成方案")
        for i, text in enumerate(GEN_TASKS):
            _check()
            yield _sse("progress", {"ctx": "generate", "index": i, "text": text, "active": True})
            await _delay("genItem")
            yield _sse("progress", {"ctx": "generate", "index": i, "text": text, "done": True})
            yield _log("INFO", text)

        execution_path = ["用户输入"] + [
            str(s.get("name") or s.get("id", "")) for s in services_meta
        ] + ["输出结果"]

        session.result = {
            "success": True,
            "executionPath": execution_path,
            "strategy": strategy,
            "scenarioDescription": body.get("scenarioDescription", ""),
            "appName": body.get("appName", ""),
            "domain": body.get("domain", "generic"),
            "domainKnowledge": body.get("domainKnowledge") or {},
            "enhancements": [],
        }

        yield _log("SUCCESS", "方案生成完成")
        yield _sse("complete", {"success": True, "metrics": metrics, "result": session.result})
        _push_record(session, metrics, True)

    except Cancelled:
        session.result = {"success": False, "cancelled": True}
        mc = {"iterations": 0, "elapsedMs": int((time.time() - session.started_at) * 1000)}
        yield _sse("complete", {"success": False, "cancelled": True, "metrics": mc, "result": session.result})

    except Exception as e:
        session.result = {"success": False, "error": str(e)}
        me = {"elapsedMs": int((time.time() - session.started_at) * 1000)}
        yield _sse("complete", {"success": False, "metrics": me, "result": session.result})
        _push_record(session, me, False)
