"""Agentic capability discovery before strict MCP contract planning.

The discovery stage deliberately uses a smaller contract than
``PackagingPlan``.  Its job is to identify evidence-grounded, user-facing
capabilities and their source boundaries.  A later planning stage remains
responsible for JSON Schema, service grouping, smoke fixtures, and deployment
contracts.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from micro_agent.core.agent import Agent
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.core.schema import AgentEvent
from micro_agent.packaging.analyzer import RepositoryIR
from micro_agent.packaging.relevance import build_relevance_evidence
from micro_agent.packaging.tools import (
    InspectRepository,
    ReadProjectFile,
    SearchProjectText,
)
from micro_agent.tool.base import Tool, ToolResult
from micro_agent.tool.registry import ToolRegistry


DISCOVERY_SCHEMA_VERSION = "ioeb.capability-discovery/v1"
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


DISCOVERY_SYSTEM_PROMPT = """你是 MCP 封装流程的能力发现 Agent。当前阶段只回答“仓库真实提供哪些适合远程 Agent 使用的业务能力，以及它们由哪些源码实现”，不要生成 server.py、Dockerfile，也不要提前编造 JSON Schema。

按以下顺序工作：
1. 先且只调用一次 inspect_repository。请求中已包含 DARP/BAGE 相关子图，先从 detailed 层确定候选，再用 search_project_text 按核心类/函数名定位测试、示例和 Notebook 用法，按需阅读 README、入口与核心实现；最多读取 10 个文件、检索 5 次。
2. 选择 1–6 个与用户意图最相关、可独立解释的用户能力。不要逐函数机械暴露；一个能力可以组合多个源码符号。也不要把不同输入输出语义的能力压成一个万能 operation。
3. 训练/推理、解析/转换、评估/解释等只有在状态、依赖和用户目的确实不同且有源码证据时才拆分。日志、文件加载、内部格式转换、health、get_model_info 等不是业务能力。
4. 每个能力必须引用真实 sourceSymbols、sourceFiles 和 evidence。必须先搜索核心符号的用法，优先引用原仓库测试、doctest、示例、Notebook 或示例资产；composition 要写清调用链、对象初始化和必要的数据转换。fixtureGuidance 必须说明一个可重复成功的最小输入从哪个仓库证据取得、如何转换；没有现成 fixture 时才允许说明使用依赖库的领域模拟器及固定种子，禁止手写随机数据。
5. sourceSymbols 必须逐字来自仓库索引中的 qualifiedName。若能力来自 Notebook 或仓库声明的外部算法依赖而索引没有可引用函数，sourceSymbols 可为空，但 sourceFiles 和 evidence 仍必须指向仓库内证据，并在 risks 说明限制。
6. 不得使用 benchmark task、ground truth、验证脚本或样例名称特判；不得把不存在的模型、checkpoint、数据文件或联网下载伪装成可用能力。缺少关键资产时记录 risks，仓库确无可调用算法时 decision=reject。
7. 完成证据收集后立即调用 save_capability_design_json。必须提交完整严格 JSON，不要只在文本中描述。

输出结构：
{
  "schemaVersion": "ioeb.capability-discovery/v1",
  "decision": "design",
  "summary": "仓库能力与边界结论",
  "capabilities": [
    {
      "name": "snake_case_name",
      "description": "面向调用者说明做什么、何时使用以及与其他能力的区别",
      "sourceSymbols": ["module.function"],
      "sourceFiles": ["relative/path.py"],
      "composition": "如何调用/组合源码，包括初始化与转换",
      "inputNotes": "真实输入语义、约束、默认值和文件内容转换",
      "outputNotes": "真实返回结构及序列化方式",
      "fixtureGuidance": "一个确定性成功输入的证据路径、提取/构造步骤和关键断言",
      "evidence": ["tests/test_core.py:42", "README.md:80"]
    }
  ],
  "excludedSymbols": [{"symbol": "module.helper", "reason": "仅内部转换"}],
  "risks": ["缺少可选模型资产等真实限制"]
}

拒绝时 decision=reject、capabilities=[]，并提供 rejectionReasons。
"""


class CapabilityDesignValidationError(ValueError):
    """Raised when a discovery candidate is not safe to pass downstream."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class CapabilityDesign:
    """Validated, immutable-by-convention output of capability discovery."""

    data: dict[str, Any]

    @classmethod
    def validate(
        cls,
        raw: dict[str, Any],
        *,
        known_symbols: set[str],
        known_files: set[str],
    ) -> "CapabilityDesign":
        if not isinstance(raw, dict):
            raise CapabilityDesignValidationError(["能力设计必须是 JSON object"])
        data = copy.deepcopy(raw)
        errors: list[str] = []
        if data.get("schemaVersion") != DISCOVERY_SCHEMA_VERSION:
            errors.append(f"schemaVersion 必须是 {DISCOVERY_SCHEMA_VERSION}")
        decision = data.get("decision")
        if decision not in {"design", "reject"}:
            errors.append("decision 必须是 design 或 reject")
        summary = data.get("summary")
        if not isinstance(summary, str) or len(summary.strip()) < 10:
            errors.append("summary 必须给出不少于 10 个字符的能力边界结论")

        capabilities = data.get("capabilities")
        if decision == "reject":
            if capabilities not in (None, []):
                errors.append("拒绝时 capabilities 必须为空")
            reasons = data.get("rejectionReasons")
            if not isinstance(reasons, list) or not any(
                isinstance(item, str) and item.strip() for item in reasons
            ):
                errors.append("拒绝时必须提供 rejectionReasons")
            if errors:
                raise CapabilityDesignValidationError(errors)
            data["capabilities"] = []
            data.setdefault("excludedSymbols", [])
            data.setdefault("risks", [])
            return cls(data)

        if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 6:
            errors.append("capabilities 必须包含 1 到 6 个能力")
            capabilities = []
        seen_names: set[str] = set()
        for index, capability in enumerate(capabilities):
            prefix = f"capabilities[{index}]"
            if not isinstance(capability, dict):
                errors.append(f"{prefix} 必须是 object")
                continue
            name = capability.get("name")
            if not isinstance(name, str) or not _SNAKE_CASE.match(name):
                errors.append(f"{prefix}.name 必须是 snake_case")
            elif name in seen_names:
                errors.append(f"能力名称重复: {name}")
            else:
                seen_names.add(name)
            description = capability.get("description")
            if not isinstance(description, str) or (
                len(description.split()) < 8
                and len(description.strip()) < 30
            ):
                errors.append(f"{prefix}.description 必须充分说明用户能力与适用时机")

            source_symbols = capability.get("sourceSymbols")
            if not isinstance(source_symbols, list) or not all(
                isinstance(symbol, str) and symbol for symbol in source_symbols
            ):
                errors.append(f"{prefix}.sourceSymbols 必须是字符串数组")
                source_symbols = []
            unknown_symbols = sorted(set(source_symbols) - known_symbols)
            if unknown_symbols:
                errors.append(
                    f"{prefix}.sourceSymbols 包含未知符号: {', '.join(unknown_symbols)}"
                )

            source_files = capability.get("sourceFiles")
            if (
                not isinstance(source_files, list)
                or not source_files
                or not all(isinstance(path, str) and path for path in source_files)
            ):
                errors.append(f"{prefix}.sourceFiles 至少引用一个仓库文件")
                source_files = []
            unknown_files = sorted(set(source_files) - known_files)
            if unknown_files:
                errors.append(
                    f"{prefix}.sourceFiles 包含未知文件: {', '.join(unknown_files)}"
                )
            if not source_symbols and not source_files:
                errors.append(f"{prefix} 没有任何源码实现证据")

            for field in (
                "composition",
                "inputNotes",
                "outputNotes",
                "fixtureGuidance",
            ):
                value = capability.get(field)
                if not isinstance(value, str) or len(value.strip()) < 8:
                    errors.append(f"{prefix}.{field} 必须给出具体源码语义")
            evidence = capability.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence
            ):
                errors.append(f"{prefix}.evidence 至少引用一个仓库证据位置")
            elif not any(_evidence_file(item) in known_files for item in evidence):
                errors.append(f"{prefix}.evidence 未引用仓库内已知文件")

        for field in ("excludedSymbols", "risks"):
            if field in data and not isinstance(data[field], list):
                errors.append(f"{field} 必须是数组")
        if errors:
            raise CapabilityDesignValidationError(errors)
        data.setdefault("excludedSymbols", [])
        data.setdefault("risks", [])
        return cls(data)

    @property
    def decision(self) -> str:
        return str(self.data["decision"])

    @property
    def capabilities(self) -> list[dict[str, Any]]:
        return list(self.data.get("capabilities", []))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=2)


def _evidence_file(reference: str) -> str:
    """Return the repository-relative file portion of ``path[:line]``."""

    candidate = reference.strip()
    match = re.match(r"^(.*?):\d+(?:-\d+)?$", candidate)
    return match.group(1) if match else candidate


@dataclass
class CapabilityDesignStore:
    """Capture validation feedback without writing untrusted candidates."""

    path: Path
    known_symbols: set[str]
    known_files: set[str]
    design: CapabilityDesign | None = None
    last_candidate: dict[str, Any] | None = None
    last_errors: list[str] | None = None

    def save(self, content: str) -> ToolResult:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            self.last_errors = [f"JSON 解析失败: line={exc.lineno}, {exc.msg}"]
            return ToolResult(error="能力设计校验失败:\n- " + self.last_errors[0])
        self.last_candidate = raw if isinstance(raw, dict) else None
        try:
            design = CapabilityDesign.validate(
                raw,
                known_symbols=self.known_symbols,
                known_files=self.known_files,
            )
        except CapabilityDesignValidationError as exc:
            self.last_errors = exc.errors
            return ToolResult(
                error="能力设计校验失败:\n- " + "\n- ".join(exc.errors)
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(design.to_json() + "\n", encoding="utf-8")
        self.design = design
        self.last_errors = []
        return ToolResult(
            output=(
                f"能力设计已保存：decision={design.decision}, "
                f"capabilities={len(design.capabilities)}"
            )
        )


class SaveCapabilityDesignJson(Tool):
    """Terminal tool for the bounded discovery stage."""

    name = "save_capability_design_json"
    description = "提交完整能力设计 JSON；系统会校验源码符号、文件、证据和能力边界。"
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "完整严格 JSON 字符串，不含 Markdown fence",
            }
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self, store: CapabilityDesignStore) -> None:
        self.store = store

    async def execute(self, **kwargs: Any) -> ToolResult:
        return self.store.save(str(kwargs.get("content", "")))


class CapabilityDiscoveryWorkflow:
    """Run Repo2MCP-style capability discovery with bounded retries."""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        ir: RepositoryIR,
        design_path: str | Path,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.ir = ir
        self.store = CapabilityDesignStore(
            path=Path(design_path).resolve(),
            known_symbols=ir.known_symbols,
            known_files={file.path for file in ir.files},
        )
        self.agent = _build_discovery_agent(self.project_dir, ir, self.store)

    def cancel(self) -> None:
        self.agent.cancel()

    async def run(self, request: str) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            type="think",
            step=0,
            data={
                "thought": (
                    "[能力发现] 先从依赖相关子图、入口、测试和示例中识别真实业务能力，"
                    "再进入严格 Schema 与服务规划。"
                )
            },
        )

        def fresh_agent() -> Agent:
            self.agent = _build_discovery_agent(
                self.project_dir,
                self.ir,
                self.store,
            )
            return self.agent

        async for event in _run_discovery(
            self.agent,
            self.store,
            self.ir,
            request,
            fresh_agent_factory=fresh_agent,
        ):
            yield event


def _build_discovery_agent(
    project_dir: Path,
    ir: RepositoryIR,
    store: CapabilityDesignStore,
) -> Agent:
    tools = ToolRegistry()
    tools.register(InspectRepository(ir, max_calls=1))
    tools.register(SearchProjectText(project_dir, max_calls=5))
    tools.register(ReadProjectFile(project_dir, max_reads=10))
    tools.register(SaveCapabilityDesignJson(store))
    return Agent(
        name="mcp_capability_discovery",
        llm=LLM(config.get_llm("reasoning")),
        tools=tools,
        system_prompt=DISCOVERY_SYSTEM_PROMPT,
        next_step_prompt=(
            "证据足够后立即调用 save_capability_design_json，"
            "不要继续读取或只在文本中总结。"
        ),
        max_steps=16,
        max_observe=50_000,
        terminal_tools={"save_capability_design_json"},
        require_terminal_tool=True,
        no_tool_retry_limit=3,
        duplicate_tool_retry_limit=4,
    )


def capability_discovery_prompt(ir: RepositoryIR, user_request: str) -> str:
    overview = {
        "fingerprint": ir.fingerprint,
        "fileCount": len(ir.files),
        "symbolCount": len(ir.symbols),
        "entrypointHints": ir.entrypointHints,
        "testFiles": ir.testFiles[:40],
        "assetFiles": ir.assetFiles[:80],
        "documentationFiles": list(ir.documentation),
        "parseErrors": ir.parseErrors,
        "truncated": ir.truncated,
        "relevanceEvidence": build_relevance_evidence(
            ir,
            user_request,
            max_tokens=18_000,
        ),
    }
    return (
        "请发现这个仓库适合封装为 MCP Tool 的真实用户能力。\n"
        f"用户意图：{user_request or '未补充；选择仓库最核心的公开能力'}\n"
        "下面是确定性的仓库索引与 DARP/BAGE 相关证据；它不包含 benchmark 答案。"
        "先核对 detailed 候选及其测试/示例，再提交完整能力设计。\n"
        + json.dumps(overview, ensure_ascii=False, indent=2)
    )


async def _run_discovery(
    agent: Agent,
    store: CapabilityDesignStore,
    ir: RepositoryIR,
    user_request: str,
    *,
    fresh_agent_factory: Callable[[], Agent] | None = None,
) -> AsyncIterator[AgentEvent]:
    initial_prompt = capability_discovery_prompt(ir, user_request)
    step_offset = 1
    for attempt in range(4):
        if attempt and fresh_agent_factory is not None:
            agent = fresh_agent_factory()
        text_candidates: list[str] = []
        if attempt == 0:
            prompt = initial_prompt
        else:
            candidate = (
                json.dumps(store.last_candidate, ensure_ascii=False, indent=2)
                if store.last_candidate is not None
                else "未捕获到可恢复候选"
            )
            prompt = (
                initial_prompt
                + "\n\n上一版能力设计未通过确定性门禁。保留已通过的能力与证据，"
                "只修正下面错误，然后重新调用 save_capability_design_json 提交完整 JSON。\n"
                + candidate
                + (
                    "\n校验错误：\n- " + "\n- ".join(store.last_errors)
                    if store.last_errors
                    else ""
                )
            )
        async for event in agent.run(prompt):
            if event.type == "think" and isinstance(event.data.get("thought"), str):
                text_candidates.append(event.data["thought"])
            elif event.type == "done" and isinstance(event.data.get("result"), str):
                text_candidates.append(event.data["result"])
            if event.type == "done":
                continue
            yield AgentEvent(
                type=event.type,
                step=step_offset + event.step,
                data=event.data,
            )
        if store.design is not None:
            return
        for candidate in reversed(text_candidates):
            recovered = _extract_json_object(candidate)
            if recovered is None:
                continue
            result = store.save(recovered)
            if store.design is not None:
                yield AgentEvent(
                    type="think",
                    step=step_offset + agent.max_steps,
                    data={
                        "thought": (
                            "[能力设计格式恢复] 模型以普通文本返回严格 JSON，"
                            "已通过同一源码证据门禁。"
                        )
                    },
                )
                return
            if result.error:
                break
        step_offset += agent.max_steps + 1
        yield AgentEvent(
            type="think",
            step=step_offset,
            data={
                "thought": (
                    "[能力发现门禁] 尚未收到有效能力设计，"
                    "保留候选与错误并进行有限重试。"
                )
            },
        )


def _extract_json_object(text: str) -> str | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else None
