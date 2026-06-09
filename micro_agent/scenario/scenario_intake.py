"""想定场景追问（grill-me 机制）：一次一问，信息足够时产出 parsedIntent。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.core.memory.persistent import FileMemory
from micro_agent.core.schema import Message, Role
from micro_agent.core.skill import SkillRegistry
from micro_agent.scenario.intent_schema import normalize_parsed_intent

_INTAKE_SYSTEM = """你是想定场景追问助手（grill-me 机制）。目标：与用户达成对业务场景的共同理解，再产出结构化想定。

规则（必须遵守）：
1. 每次只问**一个**最关键的问题；不要一次问多个。
2. 若用户首句已足够清晰（目标、关键输入/输出、成功标准可推断），直接 status=ready，不要机械凑满轮次。
3. 缺什么问什么：优先补 goal → situationBrief/情境 → 输入输出 → 约束/合规 → acceptanceCriteria（验收标准，非最终成败）。
4. 只输出**单行 JSON**，不要 markdown、不要解释。

输出格式（二选一）：
追问：{"status":"question","text":"你的单个问题","hint":"可选：给用户的回答建议（一句话）"}
就绪：{"status":"ready","text":"给用户的简短确认","userRemark":"一句话备注（用户可改，非完整想定）","scenarioSummary":"完整想定自然语言摘要","parsedIntent":{"goal":"…","situationBrief":"必要的业务情境摘要（可空）","constraints":[],"acceptanceCriteria":[],"ioExpectation":{"inputs":[],"outputs":[]}}}

parsedIntent 字段与仿真想定解析一致；situationBrief 可空字符串；constraints/acceptanceCriteria/ioExpectation 无则空数组/空对象。"""


def _domain_skill_fragment(domain: str) -> str:
    name = f"domain_{domain or 'generic'}"
    skill = SkillRegistry.get(name) or SkillRegistry.get("domain_generic")
    if not skill or not skill.prompt_fragment:
        return ""
    return f"\n\n领域知识（提问与约束时参考）：\n{skill.prompt_fragment[:4000]}\n"


def _parse_intake_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def run_scenario_intake_turn(
    *,
    message: str,
    domain: str = "generic",
    session_id: str | None = None,
) -> dict[str, Any]:
    """处理一轮用户输入，返回 question 或 ready。"""
    text = (message or "").strip()
    if not text:
        return {
            "status": "question",
            "text": "请先描述你想完成的业务场景或任务目标。",
            "session_id": session_id or "",
        }

    sid = session_id or uuid.uuid4().hex[:12]
    memory_dir = Path(config.workspace) / config.memory.storage_dir
    memory = FileMemory(memory_dir)
    await memory.load(sid)
    memory.add(Message(role=Role.USER, content=text))

    system = _INTAKE_SYSTEM + _domain_skill_fragment(domain)
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in memory.to_list():
        role = item.get("role", "user")
        content = item.get("content") or ""
        if content:
            messages.append({"role": role, "content": content})

    llm = LLM(config.llm)
    try:
        resp = await llm.complete(messages, temperature=0.3)
        raw = resp.content or ""
    except Exception as exc:
        logger.warning(f"想定追问 LLM 失败: {exc}")
        raise

    memory.add(Message(role=Role.ASSISTANT, content=raw))
    await memory.persist()

    parsed = _parse_intake_json(raw)
    if not parsed:
        return {
            "status": "question",
            "text": "能再具体说明一下你的业务目标和期望输出吗？",
            "session_id": sid,
        }

    status = str(parsed.get("status") or "").strip().lower()
    if status == "ready":
        intent = normalize_parsed_intent(parsed.get("parsedIntent"))
        if not intent["goal"]:
            intent["goal"] = str(parsed.get("scenarioSummary") or text)[:300]
        summary = str(parsed.get("scenarioSummary") or "").strip() or intent["goal"]
        intent["parserModel"] = llm.model
        intent["parsedAt"] = datetime.now(timezone.utc).isoformat()
        intent["intakeSessionId"] = sid
        return {
            "status": "ready",
            "text": str(parsed.get("text") or "想定信息已足够，开始匹配服务。"),
            "userRemark": str(parsed.get("userRemark") or summary[:120]),
            "scenarioSummary": summary,
            "parsedIntent": intent,
            "session_id": sid,
        }

    question = str(parsed.get("text") or "").strip()
    if not question:
        question = "能补充一下关键输入数据和期望输出形式吗？"
    out: dict[str, Any] = {
        "status": "question",
        "text": question,
        "session_id": sid,
    }
    hint = str(parsed.get("hint") or "").strip()
    if hint:
        out["hint"] = hint
    return out
