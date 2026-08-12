"""想定场景追问（grill-me 机制）：一次一问，信息足够时产出 ScenarioParsed。"""

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
from micro_agent.scenario.schema import ScenarioSource, normalize_scenario_parsed

_INTAKE_SYSTEM = """你是想定场景追问助手（grill-me 机制）。目标：与用户达成对业务场景的共同理解，再产出结构化想定。

规则（必须遵守）：
1. 每次只问**一个**最关键的问题；不要一次问多个。
2. 若用户首句已足够清晰（目标、关键输入/输出、成功标准可推断），直接 status=ready，不要机械凑满轮次。
3. 缺什么问什么：优先补 goal → description/情境 → 约束/合规 → acceptanceCriteria（验收标准，非最终成败）。
4. 只输出**单行 JSON**，不要 markdown、不要解释。

输出格式（二选一）：
追问：{"status":"question","text":"你的单个问题","hint":"可选：给用户的回答建议（一句话）"}
就绪：{"status":"ready","text":"给用户的简短确认","userRemark":"一句话备注（用户可改，非完整想定）","scenarioSummary":"完整想定自然语言摘要","scenarioParsed":{"goal":"…","description":"完整场景描述","constraints":[],"acceptanceCriteria":[],"domain":__DOMAIN_JSON__}}

scenarioParsed 字段与仿真想定解析一致；description 为一段话场景描述；constraints/acceptanceCriteria 无则空数组。"""

_GOAL_MARKERS = ("目标", "我要", "希望", "构建", "开发", "创建")
_INPUT_MARKERS = ("输入", "接收", "数据源")
_OUTPUT_MARKERS = ("输出", "产出", "返回", "生成")
_ACCEPTANCE_MARKERS = ("验收", "成功标准", "评价指标", "达到以下", "能够", "能识别")


class ScenarioDomainError(ValueError):
    pass


def _require_business_domain(domain: str) -> str:
    requested_domain = str(domain or "").strip()
    if not requested_domain:
        raise ScenarioDomainError("场景追问必须提供业务领域")
    if requested_domain == "generic":
        raise ScenarioDomainError("generic 是内部兜底值，不能作为业务领域")

    skill_name = f"domain_{requested_domain}"
    skills_dir = Path(config.workspace) / config.skills.directory
    skill_dir = skills_dir / skill_name
    if not SkillRegistry.get(skill_name) and not (
        (skill_dir / "SKILL.md").exists() or (skill_dir / "skill.toml").exists()
    ):
        raise ScenarioDomainError(f"未配置业务领域: {requested_domain}")
    return requested_domain


def _build_intake_system(domain: str) -> str:
    requested_domain = str(domain or "").strip()
    domain_json = json.dumps(requested_domain, ensure_ascii=False)
    return (
        _INTAKE_SYSTEM.replace("__DOMAIN_JSON__", domain_json)
        + f"\n5. 当前请求领域代码为 {domain_json}，scenarioParsed.domain 必须原样使用该值。"
    )


def _domain_skill_fragment(domain: str) -> str:
    name = f"domain_{domain}"
    skill = SkillRegistry.get(name)
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


def _is_valid_intake_payload(parsed: dict[str, Any] | None) -> bool:
    if not parsed:
        return False
    status = str(parsed.get("status") or "").strip().lower()
    if status == "question":
        return bool(str(parsed.get("text") or "").strip())
    if status == "ready":
        return isinstance(parsed.get("scenarioParsed"), dict)
    return False


def _has_explicit_scenario_contract(items: list[dict[str, Any]]) -> bool:
    user_text = "\n".join(
        str(item.get("content") or "")
        for item in items
        if item.get("role") == "user" and item.get("content")
    )
    if len(user_text.strip()) < 30:
        return False
    marker_groups = (
        _GOAL_MARKERS,
        _INPUT_MARKERS,
        _OUTPUT_MARKERS,
        _ACCEPTANCE_MARKERS,
    )
    return all(any(marker in user_text for marker in markers) for markers in marker_groups)


async def run_scenario_intake_turn(
    *,
    message: str,
    domain: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """处理一轮用户输入，返回 question 或 ready。"""
    domain = _require_business_domain(domain)
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

    system = _build_intake_system(domain) + _domain_skill_fragment(domain)
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

    parsed = _parse_intake_json(raw)
    explicit_contract = _has_explicit_scenario_contract(memory.to_list())
    status = str((parsed or {}).get("status") or "").strip().lower()
    retry_reason = ""
    if not _is_valid_intake_payload(parsed):
        retry_reason = "上一条响应不是符合约定的 question/ready JSON。"
    elif explicit_contract and status == "question":
        retry_reason = "用户已经明确提供目标、输入、输出和验收标准，不应继续追问。"

    if retry_reason:
        logger.warning(f"想定追问响应需纠正 [session={sid}, domain={domain}]: {retry_reason}")
        retry_messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"{retry_reason} 请严格按系统约定只重发单行 JSON；"
                    "不要解释，不要重复询问用户已经给出的信息。"
                ),
            },
        ]
        resp = await llm.complete(retry_messages, temperature=0)
        raw = resp.content or ""
        parsed = _parse_intake_json(raw)
        status = str((parsed or {}).get("status") or "").strip().lower()

    memory.add(Message(role=Role.ASSISTANT, content=raw))
    await memory.persist()

    if not _is_valid_intake_payload(parsed):
        raise ValueError("想定追问模型未返回有效的结构化结果")
    if explicit_contract and status != "ready":
        raise ValueError("想定信息已完整，但模型仍错误要求追问")

    if status == "ready":
        # 收集对话原文作为证据
        dialogue = [
            {"role": item.get("role", "user"), "content": item.get("content", "")}
            for item in memory.to_list()
            if item.get("content")
        ]

        raw_sp = parsed.get("scenarioParsed") or {}
        if not isinstance(raw_sp, dict):
            raw_sp = {}

        summary = str(parsed.get("scenarioSummary") or "").strip()
        scenario_parsed = normalize_scenario_parsed(
            {**raw_sp, "description": raw_sp.get("description") or summary},
            raw_user_input=text,
            intake_dialogue=dialogue,
            intake_session_id=sid,
            parser_model=llm.model,
            parsed_at=datetime.now(timezone.utc).isoformat(),
            domain=domain,
        )
        if not scenario_parsed.goal:
            scenario_parsed.goal = (summary or text)[:300]
        if not scenario_parsed.description:
            scenario_parsed.description = summary or scenario_parsed.goal

        summary = summary or scenario_parsed.description or scenario_parsed.goal

        return {
            "status": "ready",
            "text": str(parsed.get("text") or "想定信息已足够，开始匹配服务。"),
            "userRemark": str(parsed.get("userRemark") or summary[:120]),
            "scenarioSummary": summary,
            "scenarioParsed": scenario_parsed.to_dict(),
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
