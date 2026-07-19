"""Path-contained tools exposed to the packaging Agents instead of host Bash."""

from __future__ import annotations

import ast
import difflib
import io
import json
import re
import tokenize
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from micro_agent.packaging.analyzer import RepositoryIR
from micro_agent.packaging.capability_coverage import (
    assess_dispatch_coverage,
    strategy_fixes_dispatch_value,
)
from micro_agent.packaging.interface_quality import (
    InterfaceQualityReport,
    assess_interface_quality,
)
from micro_agent.packaging.models import (
    PLAN_JSON_SCHEMA,
    SCHEMA_VERSION,
    PackagingPlan,
    PlanValidationError,
)
from micro_agent.packaging.scaffold import (
    _runtime_requirement_contract,
    _source_owned_distributions,
)
from micro_agent.packaging.verifier import ArtifactVerifier
from micro_agent.tool.base import Tool, ToolResult


def _contained_path(root: Path, relative: str) -> Path:
    if not relative or "\x00" in relative:
        raise ValueError("文件路径不能为空")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("文件路径越界")
    return candidate


class InspectRepository(Tool):
    name = "inspect_repository"
    description = "读取全仓库静态清单：文件、函数/类/方法、签名、调用、入口、测试、资产和 README 摘要。"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, ir: RepositoryIR, *, max_calls: int | None = None) -> None:
        self.ir = ir
        self.max_calls = max_calls
        self.calls = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        if self.max_calls is not None and self.calls > self.max_calls:
            return ToolResult(error="仓库完整清单已读取过；请使用已有证据继续规划")
        return ToolResult(output=self.ir.to_json(indent=None))


class ReadProjectFile(Tool):
    name = "read_project_file"
    description = "按仓库相对路径读取源码、测试、配置或文档；路径严格限制在用户提交目录内。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "仓库相对路径"},
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "end_line": {"type": "integer", "minimum": 1, "default": 400},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        project_dir: str | Path,
        *,
        max_chars: int = 60_000,
        max_reads: int | None = None,
    ) -> None:
        self.root = Path(project_dir).resolve()
        self.max_chars = max_chars
        self.max_reads = max_reads
        self.calls = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self.max_reads is not None and self.calls >= self.max_reads:
            return ToolResult(
                error=(
                    f"本轮源码读取上限为 {self.max_reads} 个文件，额度已用完；"
                    "不得再次调用 read_project_file，必须立即完成当前阶段要求的规划或产物"
                )
            )
        try:
            path = _contained_path(self.root, str(kwargs.get("path", "")))
        except ValueError as exc:
            return ToolResult(error=str(exc))
        if not path.is_file() or path.is_symlink():
            requested = str(kwargs.get("path", ""))
            if requested.startswith("algorithm/"):
                corrected = requested.removeprefix("algorithm/")
                return ToolResult(
                    error=(
                        "read_project_file 的路径相对用户提交根目录，不能带 algorithm/ 前缀；"
                        + (
                            f"请改用 {corrected}"
                            if corrected
                            else "请直接填写真实文件路径；该工具不支持目录浏览"
                        )
                    )
                )
            suggestions = _file_path_suggestions(self.root, requested)
            suggestion_text = (
                "；仓库内可能的真实路径: "
                + ", ".join(suggestions)
                + "。请从候选中选择，不要继续重复不存在的路径"
                if suggestions
                else ""
            )
            return ToolResult(
                error=(
                    f"文件不存在或不可读: {kwargs.get('path', '')}"
                    + suggestion_text
                )
            )
        self.calls += 1
        start = max(1, int(kwargs.get("start_line", 1)))
        end = max(start, min(start + 999, int(kwargs.get("end_line", 400))))
        text = path.read_text(encoding="utf-8", errors="replace")
        selected = "\n".join(text.splitlines()[start - 1:end])
        if len(selected) > self.max_chars:
            selected = selected[: self.max_chars] + "\n...(truncated)"
        return ToolResult(output=f"# {path.relative_to(self.root)} lines {start}-{end}\n{selected}")


def _file_path_suggestions(
    root: Path,
    requested: str,
    *,
    limit: int = 5,
) -> list[str]:
    """Suggest contained files for omitted src/ prefixes or close paths."""

    normalized = requested.strip().lstrip("./")
    name = Path(normalized).name
    if not name:
        return []
    candidates: list[tuple[int, float, str]] = []
    try:
        matches = root.rglob(name)
        for path in matches:
            if (
                not path.is_file()
                or path.is_symlink()
                or any(
                    part in {".git", ".venv", "venv", "__pycache__"}
                    for part in path.relative_to(root).parts
                )
            ):
                continue
            relative = path.relative_to(root).as_posix()
            suffix_match = int(
                not (
                    relative == normalized
                    or relative.endswith("/" + normalized)
                )
            )
            similarity = difflib.SequenceMatcher(
                None,
                normalized,
                relative,
            ).ratio()
            candidates.append((suffix_match, -similarity, relative))
    except OSError:
        return []
    candidates.sort()
    return [relative for _, _, relative in candidates[:limit]]


@dataclass
class PlanStore:
    path: Path
    known_symbols: set[str]
    known_files: set[str] | None = None
    symbol_required_parameters: dict[str, list[str]] | None = None
    symbol_calls: dict[str, list[str]] | None = None
    symbol_is_generator: dict[str, bool] | None = None
    symbol_dispatch_branches: dict[str, list[dict[str, Any]]] | None = None
    candidate_symbols: set[str] | None = None
    enforce_interface_quality: bool = False
    require_independent_smoke_evidence: bool = False
    smoke_evidence_root: Path | None = None
    rejected_smoke_inputs: dict[str, set[str]] | None = None
    verified_contract_records: list[dict[str, Any]] | None = None
    contract_smoke_grounded_tools: list[str] | None = None
    smoke_revision_attempted: bool = False
    plan: PackagingPlan | None = None
    last_candidate: dict[str, Any] | None = None
    last_errors: list[str] | None = None
    best_candidate: dict[str, Any] | None = None
    best_errors: list[str] | None = None
    best_score: tuple[int, int] | None = None
    interface_quality: InterfaceQualityReport | None = None


class SavePackagingPlan(Tool):
    name = "save_packaging_plan"
    description = "提交语义封装规划。规划会立即做结构、源码证据、工具唯一性和依赖校验；错误必须修复后重提。"
    parameters = PLAN_JSON_SCHEMA

    def __init__(self, store: PlanStore) -> None:
        self.store = store

    async def execute(self, **kwargs: Any) -> ToolResult:
        normalized = dict(kwargs)
        for field in ("services", "excludedSymbols", "assumptions", "riskNotes", "rejectionReasons"):
            value = normalized.get(field)
            if not isinstance(value, str):
                continue
            if value.strip().startswith(("[", "{")):
                parsed = _parse_structured_string(value)
                if parsed is not None:
                    normalized[field] = parsed
            elif field in {"excludedSymbols", "assumptions", "riskNotes", "rejectionReasons"}:
                normalized[field] = [
                    re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
                    for line in value.splitlines()
                    if re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
                ]
        if isinstance(normalized.get("services"), str):
            self.store.plan = None
            errors = [
                "services 被模型序列化成了无法解析的字符串；请改用 save_packaging_plan_json 提交整份严格 JSON"
            ]
            _record_rejected_candidate(
                self.store,
                normalized,
                errors,
                stage="shape",
            )
            return ToolResult(error=errors[0])
        self.store.contract_smoke_grounded_tools = (
            _ground_plan_smoke_from_verified_contract(normalized, self.store)
        )
        self.store.last_candidate = json.loads(
            json.dumps(normalized, ensure_ascii=False)
        )
        try:
            plan = PackagingPlan.validate(
                normalized,
                known_symbols=self.store.known_symbols,
                known_files=self.store.known_files,
                symbol_required_parameters=self.store.symbol_required_parameters,
                symbol_calls=self.store.symbol_calls,
                symbol_is_generator=self.store.symbol_is_generator,
                candidate_symbols=self.store.candidate_symbols,
            )
        except PlanValidationError as exc:
            self.store.plan = None
            errors = _augment_unknown_symbol_errors(
                exc.errors,
                self.store.known_symbols,
            )
            _record_rejected_candidate(
                self.store,
                normalized,
                errors,
                stage="structure",
            )
            self.store.interface_quality = None
            return ToolResult(
                error=(
                    "规划校验失败。save_packaging_plan 不是 PATCH；下一次必须重新提交包含 "
                    "schemaVersion、decision、analysisSummary、services 在内的完整规划:\n- "
                    + "\n- ".join(errors)
                )
            )
        if plan.decision == "package" and self.store.verified_contract_records:
            contract_errors = _verified_contract_alignment_errors(
                plan,
                self.store,
            )
            if contract_errors:
                self.store.plan = None
                _record_rejected_candidate(
                    self.store,
                    normalized,
                    contract_errors,
                    stage="contract",
                )
                self.store.interface_quality = None
                return ToolResult(
                    error=(
                        "已验证模板契约对齐门禁失败。Tool 必须通过运行时已验证的 "
                        "main.main_process 入口，并保持分支输入与公开 Schema 可映射；"
                        "请修订接口规划后重新提交完整规划:\n- "
                        + "\n- ".join(contract_errors)
                    )
                )
        if self.store.enforce_interface_quality and plan.decision == "package":
            quality = assess_interface_quality(plan)
            self.store.interface_quality = quality
            if not quality.passed:
                self.store.plan = None
                _record_rejected_candidate(
                    self.store,
                    normalized,
                    quality.errors,
                    stage="interface",
                )
                return ToolResult(
                    error=(
                        "Agent-facing MCP 接口质量门禁失败。不得删除工具或编造约束；"
                        "必须依据仓库证据补齐描述、真实约束与输出语义后，"
                        "重新提交完整规划:\n- "
                        + "\n- ".join(quality.errors)
                    )
                )
        if plan.decision == "package" and self.store.require_independent_smoke_evidence:
            smoke_errors = _independent_smoke_evidence_errors(
                plan,
                self.store.known_files or set(),
                evidence_root=self.store.smoke_evidence_root,
            )
            if smoke_errors:
                self.store.plan = None
                smoke_stage = (
                    "smoke_provenance"
                    if _smoke_errors_prove_fixture_grounding(smoke_errors)
                    else "smoke"
                )
                _record_rejected_candidate(
                    self.store,
                    normalized,
                    smoke_errors,
                    stage=smoke_stage,
                )
                return ToolResult(
                    error=(
                        "模板适配仓库的 smoke 证据门禁失败。main.py 是后加的薄适配层，"
                        "不能自行证明示意输入可执行；必须引用原仓库测试、doctest 或示例，"
                        "并重新提交完整规划:\n- "
                        + "\n- ".join(smoke_errors)
                    )
                )
        if plan.decision == "package" and self.store.symbol_dispatch_branches:
            dispatch_errors = assess_dispatch_coverage(
                plan, self.store.symbol_dispatch_branches
            )
            if dispatch_errors:
                self.store.plan = None
                _record_rejected_candidate(
                    self.store,
                    normalized,
                    dispatch_errors,
                    stage="dispatch",
                )
                return ToolResult(
                    error=(
                        "分支能力覆盖门禁失败。必须依据静态分派分支拆分 Agent 可选择的 Tool，"
                        "并在 adapterStrategy 中写明固定参数值后重新提交完整规划:\n- "
                        + "\n- ".join(dispatch_errors)
                    )
                )
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(plan.to_json() + "\n", encoding="utf-8")
        self.store.plan = plan
        self.store.last_candidate = plan.to_dict()
        self.store.last_errors = None
        self.store.best_candidate = plan.to_dict()
        self.store.best_errors = None
        self.store.best_score = (5, 0)
        return ToolResult(
            output=(
                f"规划已保存：decision={plan.decision}, "
                f"services={len(plan.data.get('services', []))}, tools={len(plan.tools)}"
                + (
                    ", verifiedContractSmoke="
                    + ",".join(self.store.contract_smoke_grounded_tools)
                    if self.store.contract_smoke_grounded_tools
                    else ""
                )
                + (
                    f", interfaceGoE={self.store.interface_quality.metrics['referenceFreeGoE']}"
                    if self.store.interface_quality is not None
                    else ""
                )
            )
        )


def _ground_plan_smoke_from_verified_contract(
    raw: dict[str, Any],
    store: PlanStore,
) -> list[str]:
    """Replace model-invented smoke fixtures with runtime-verified contracts.

    Template adaptation already executes each submitted fixture in an isolated
    container. Planning therefore must not ask the LLM to reproduce the same
    structured value exactly. This gate maps a Tool's fixed dispatch branch to
    the corresponding verified public input before any plan validation runs.
    """

    records = store.verified_contract_records or []
    if raw.get("decision") != "package" or not records:
        return []
    rejected = store.rejected_smoke_inputs or {}
    grounded: list[str] = []
    services = raw.get("services")
    if not isinstance(services, list):
        return grounded
    for service in services:
        if not isinstance(service, dict):
            continue
        tools = service.get("tools")
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            source_symbols = tool.get("sourceSymbols")
            if not isinstance(source_symbols, list) or "main.main_process" not in source_symbols:
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            schema = tool.get("inputSchema")
            properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
            required = schema.get("required", []) if isinstance(schema, dict) else []
            if not isinstance(properties, dict) or not isinstance(required, list):
                continue
            candidates: list[dict[str, Any]] = []
            for record in _contract_branch_records(tool, records):
                smoke_input = record["toolSmokeInput"]
                if (
                    not set(smoke_input).issubset(properties)
                    or not set(required).issubset(smoke_input)
                    or _contract_record_was_rejected(name, record, rejected)
                ):
                    continue
                candidates.append(record)
            if not candidates:
                continue
            selected = min(
                candidates,
                key=lambda item: _canonical_smoke_input(item["toolSmokeInput"]),
            )
            tool["smokeTest"] = {
                "enabled": True,
                "input": json.loads(
                    json.dumps(selected["toolSmokeInput"], ensure_ascii=False)
                ),
                "evidence": list(selected["evidence"]),
            }
            grounded.append(name)
    return grounded


def _contract_branch_records(
    tool: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    strategy = str(tool.get("adapterStrategy", ""))
    properties = tool.get("inputSchema", {}).get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    matched: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        smoke_input = record.get("toolSmokeInput")
        evidence = record.get("evidence")
        if (
            not isinstance(smoke_input, dict)
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item for item in evidence)
        ):
            continue
        raw_bindings = record.get("dispatchBindings")
        if isinstance(raw_bindings, list):
            bindings = [
                binding
                for binding in raw_bindings
                if isinstance(binding, dict)
                and isinstance(binding.get("parameter"), str)
                and "value" in binding
            ]
        else:
            parameter = record.get("dispatchParameter")
            bindings = (
                [{"parameter": parameter, "value": record.get("dispatchValue")}]
                if isinstance(parameter, str)
                else []
            )
        if not bindings:
            matched.append(record)
            continue
        if all(
            binding["parameter"] not in properties
            and strategy_fixes_dispatch_value(
                strategy,
                binding["parameter"],
                binding["value"],
            )
            for binding in bindings
        ):
            matched.append(record)
    return matched


def _contract_record_was_rejected(
    tool_name: str,
    record: dict[str, Any],
    rejected: dict[str, set[str]],
) -> bool:
    return _canonical_smoke_input(record["toolSmokeInput"]) in rejected.get(
        tool_name,
        set(),
    )


def _verified_contract_alignment_errors(
    plan: PackagingPlan,
    store: PlanStore,
) -> list[str]:
    records = store.verified_contract_records or []
    rejected = store.rejected_smoke_inputs or {}
    errors: list[str] = []
    for tool in plan.tools:
        name = str(tool.get("name", "<unnamed>"))
        source_symbols = tool.get("sourceSymbols", [])
        if "main.main_process" not in source_symbols:
            errors.append(
                f"[verified_contract_source] {name} 未把运行时已验证的 "
                "main.main_process 作为 sourceSymbol；模板能力不能绕过公共契约"
            )
            continue
        branch_records = _contract_branch_records(tool, records)
        if not branch_records:
            errors.append(
                f"[verified_contract_branch] {name} 的 adapterStrategy 未匹配任何"
                "运行时已验证分支，或仍公开了应由 Tool 固定的分派参数"
            )
            continue
        available = [
            record
            for record in branch_records
            if not _contract_record_was_rejected(name, record, rejected)
        ]
        if not available:
            # Runtime evidence has superseded these fixtures. The bounded smoke
            # revision flow may select another independently grounded example.
            continue
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        fitting = [
            record
            for record in available
            if isinstance(properties, dict)
            and isinstance(required, list)
            and set(record["toolSmokeInput"]).issubset(properties)
            and set(required).issubset(record["toolSmokeInput"])
        ]
        if not fitting:
            fixture_keys = sorted(
                {
                    key
                    for record in available
                    for key in record["toolSmokeInput"]
                }
            )
            errors.append(
                f"[verified_contract_schema] {name} 的 inputSchema 无法承载已验证"
                f"公开输入；契约字段={fixture_keys}, "
                f"schema 字段={sorted(properties) if isinstance(properties, dict) else []}, "
                f"required={sorted(required) if isinstance(required, list) else []}"
            )
            continue
        smoke = tool.get("smokeTest", {})
        smoke_input = smoke.get("input") if isinstance(smoke, dict) else None
        expected = {
            _canonical_smoke_input(record["toolSmokeInput"])
            for record in fitting
        }
        if (
            not smoke.get("enabled")
            or not isinstance(smoke_input, dict)
            or _canonical_smoke_input(smoke_input) not in expected
        ):
            errors.append(
                f"[verified_contract_smoke] {name}.smokeTest 未使用匹配分支中"
                "已在隔离容器执行成功的公开输入"
            )
    return errors


def _record_rejected_candidate(
    store: PlanStore,
    candidate: dict[str, Any],
    errors: list[str],
    *,
    stage: str,
) -> None:
    cloned = json.loads(json.dumps(candidate, ensure_ascii=False))
    cloned_errors = list(errors)
    store.last_candidate = cloned
    store.last_errors = cloned_errors
    stage_rank = {
        "shape": 0,
        "structure": 1,
        "contract": 2,
        "interface": 2,
        "smoke": 3,
        "smoke_provenance": 4,
        "dispatch": 5,
    }[stage]
    score = (stage_rank, -len(cloned_errors))
    if store.best_score is None or score > store.best_score:
        store.best_candidate = cloned
        store.best_errors = cloned_errors
        store.best_score = score


class SavePackagingPlanJson(Tool):
    name = "save_packaging_plan_json"
    description = (
        "当复杂 services 被函数调用序列化损坏时，以一段严格 JSON 文本提交完整规划；"
        "内容仍执行与 save_packaging_plan 完全相同的所有质量门禁。"
        "excludedSymbols 是规划根节点字段，不能放在 services 内。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "完整规划 JSON；不能使用 Markdown code fence、Python repr 或局部 PATCH",
            }
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self, store: PlanStore) -> None:
        self.store = store

    async def execute(self, **kwargs: Any) -> ToolResult:
        content = str(kwargs.get("content", "")).strip()
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            self.store.plan = None
            self.store.last_errors = [
                f"完整规划不是严格 JSON: line={exc.lineno}, column={exc.colno}, {exc.msg}"
            ]
            return ToolResult(error=self.store.last_errors[0])
        if not isinstance(raw, dict):
            self.store.plan = None
            self.store.last_errors = ["完整规划 JSON 顶层必须是 object"]
            return ToolResult(error=self.store.last_errors[0])
        _canonicalize_nonsemantic_shape(raw)
        _drop_unknown_exclusions(raw, self.store.known_symbols)
        return await SavePackagingPlan(self.store).execute(**raw)


class ReviseSmokeTests(Tool):
    """Allow a runtime repair turn to change only reviewed smoke fixtures."""

    name = "revise_smoke_tests"
    description = (
        "只提交失败 Tool 的 smokeTest.input/evidence 局部修订；"
        "系统会将其确定性合并到已经审核通过的完整规划。"
        "用于容器日志证明原 smoke 输入与真实入口不兼容、且仓库中存在另一组可追溯可执行输入时；"
        "服务边界、工具名、Schema、adapterStrategy 与其他字段必须保持不变。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "revisions": {
                "type": "array",
                "minItems": 1,
                "description": "仅列出需要更换 smoke fixture 的失败工具",
                "items": {
                    "type": "object",
                    "properties": {
                        "toolName": {
                            "type": "string",
                            "description": "已审核规划中现有的精确 Tool 名称",
                        },
                        "input": {
                            "type": "object",
                            "description": "符合该 Tool inputSchema 的完整可执行输入",
                        },
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "description": "新输入来自真实测试/doctest/示例的 file:line 证据",
                        },
                    },
                    "required": ["toolName", "input", "evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["revisions"],
        "additionalProperties": False,
    }

    def __init__(self, store: PlanStore, current_plan: PackagingPlan) -> None:
        self.store = store
        self.current_plan = current_plan

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.store.smoke_revision_attempted = True
        revisions = kwargs.get("revisions")
        if isinstance(revisions, str):
            parsed = _parse_structured_string(revisions)
            if isinstance(parsed, list):
                revisions = parsed
        if not isinstance(revisions, list) or not revisions:
            return ToolResult(error="revisions 必须包含至少一个失败 Tool 的局部 smoke 修订")

        current_raw = self.current_plan.to_dict()
        tools_by_name = {
            str(tool.get("name")): tool
            for service in current_raw.get("services", [])
            for tool in service.get("tools", [])
        }
        seen: set[str] = set()
        prepared: list[tuple[str, dict[str, Any]]] = []
        for index, revision in enumerate(revisions):
            if not isinstance(revision, dict):
                return ToolResult(error=f"revisions[{index}] 必须是 object")
            unknown_fields = sorted(
                set(revision) - {"toolName", "input", "evidence"}
            )
            if unknown_fields:
                return ToolResult(
                    error=(
                        f"revisions[{index}] 包含不允许字段: {unknown_fields}；"
                        "只能提交 toolName/input/evidence"
                    )
                )
            tool_name = str(revision.get("toolName", "")).strip()
            if tool_name not in tools_by_name:
                return ToolResult(
                    error=(
                        f"未知 Tool: {tool_name or '<empty>'}；"
                        f"只能修订: {sorted(tools_by_name)}"
                    )
                )
            if tool_name in seen:
                return ToolResult(error=f"Tool {tool_name} 在 revisions 中重复")
            seen.add(tool_name)
            smoke_input = revision.get("input")
            evidence = revision.get("evidence")
            if not isinstance(smoke_input, dict):
                return ToolResult(error=f"Tool {tool_name} 的 input 必须是 object")
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(item, str) or not item.strip() for item in evidence)
            ):
                return ToolResult(
                    error=f"Tool {tool_name} 的 evidence 必须是非空字符串数组"
                )
            if not isinstance(tools_by_name[tool_name].get("smokeTest"), dict):
                return ToolResult(error=f"Tool {tool_name} 没有可修订的 smokeTest")
            prepared.append(
                (
                    tool_name,
                    {
                        "enabled": True,
                        "input": json.loads(
                            json.dumps(smoke_input, ensure_ascii=False)
                        ),
                        "evidence": [item.strip() for item in evidence],
                    },
                )
            )

        accepted_plan = self.current_plan
        accepted_names: list[str] = []
        rejected: list[str] = []
        auto_grounded: list[str] = []
        accepted_quality: InterfaceQualityReport | None = None
        for index, (tool_name, smoke_update) in enumerate(prepared):
            rejected_inputs = (self.store.rejected_smoke_inputs or {}).get(
                tool_name, set()
            )
            if _canonical_smoke_input(smoke_update["input"]) in rejected_inputs:
                rejected.append(
                    f"{tool_name}: 该完整 input 已被外层隔离容器实际执行并判定失败；"
                    "必须选择不同的、由真实测试/doctest/示例支持的完整 fixture"
                )
                continue
            candidate_raw = accepted_plan.to_dict()
            candidate_tool = next(
                tool
                for service in candidate_raw.get("services", [])
                for tool in service.get("tools", [])
                if str(tool.get("name")) == tool_name
            )
            previous_smoke = candidate_tool["smokeTest"]
            updated_smoke = dict(previous_smoke)
            updated_smoke.update(smoke_update)
            if updated_smoke == previous_smoke:
                rejected.append(
                    f"{tool_name}: fixture 与当前规划完全相同"
                )
                continue
            candidate_tool["smokeTest"] = updated_smoke
            check_path = self.store.path.with_name(
                f".{self.store.path.name}.smoke-{index}.tmp"
            )
            check_store = _smoke_check_store(self.store, check_path)
            try:
                result = await SavePackagingPlan(check_store).execute(
                    **candidate_raw
                )
                if (
                    not rejected_inputs
                    and result.error
                    and (
                        "[smoke_fixture_grounding]" in result.error
                        or "[smoke_evidence_reference]" in result.error
                    )
                ):
                    grounded = _ground_smoke_revision_from_repository(
                        candidate_raw,
                        tool_name,
                        self.store,
                    )
                    if grounded is not None:
                        candidate_raw = grounded
                        check_store = _smoke_check_store(
                            self.store,
                            check_path,
                        )
                        result = await SavePackagingPlan(check_store).execute(
                            **candidate_raw
                        )
                        grounded_tool = next(
                            item
                            for service in candidate_raw.get("services", [])
                            for item in service.get("tools", [])
                            if str(item.get("name")) == tool_name
                        )
                        grounded_input = grounded_tool["smokeTest"]["input"]
                        if _canonical_smoke_input(grounded_input) in rejected_inputs:
                            result = ToolResult(
                                error=(
                                    f"{tool_name}: 自动落地候选已被隔离容器实际执行并判定失败，"
                                    "不能回退到该 fixture"
                                )
                            )
                            check_store.plan = None
                        elif not result.error and check_store.plan is not None:
                            auto_grounded.append(tool_name)
            finally:
                check_path.unlink(missing_ok=True)
            if result.error or check_store.plan is None:
                rejected.append(
                    f"{tool_name}: {result.error or '未生成有效规划'}"
                )
                continue
            accepted_plan = check_store.plan
            accepted_quality = check_store.interface_quality
            accepted_names.append(tool_name)

        if not accepted_names:
            self.store.last_candidate = self.current_plan.to_dict()
            self.store.last_errors = rejected
            return ToolResult(
                error=(
                    "没有 smoke fixture 通过规划门禁:\n- "
                    + "\n- ".join(rejected)
                )
            )

        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(
            accepted_plan.to_json() + "\n",
            encoding="utf-8",
        )
        self.store.plan = accepted_plan
        self.store.last_candidate = accepted_plan.to_dict()
        self.store.last_errors = None
        self.store.best_candidate = accepted_plan.to_dict()
        self.store.best_errors = None
        self.store.best_score = (5, 0)
        self.store.interface_quality = accepted_quality
        rejected_note = (
            "；其余修订未应用: "
            + " | ".join(item[:500] for item in rejected)
            if rejected
            else ""
        )
        grounding_note = (
            "；已将以下工具的自由文本输入机械替换为原仓库测试/doctest/示例中"
            "最接近且有 file:line 的真实 fixture，仍需通过外层容器: "
            + ", ".join(auto_grounded)
            if auto_grounded
            else ""
        )
        return ToolResult(
            output=(
                f"已确定性更新 {', '.join(accepted_names)} 的 smokeTest.input/evidence，"
                "并通过全部规划门禁；外层将用新输入重新执行静态与容器验收"
                + grounding_note
                + rejected_note
            )
        )


def _canonical_smoke_input(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _smoke_check_store(store: PlanStore, path: Path) -> PlanStore:
    return replace(
        store,
        path=path,
        plan=None,
        last_candidate=None,
        last_errors=None,
        best_candidate=None,
        best_errors=None,
        best_score=None,
        interface_quality=None,
    )


def _ground_smoke_revision_from_repository(
    raw: dict[str, Any],
    tool_name: str,
    store: PlanStore,
) -> dict[str, Any] | None:
    root = store.smoke_evidence_root
    if root is None or not store.known_files:
        return None
    generated_files = {
        "main.py",
        "README.ioeb.md",
        "template_adaptation.json",
    }
    candidate_paths = _smoke_candidate_files(
        set(store.known_files) - generated_files
    )
    corpus = _read_smoke_evidence_corpus(root, candidate_paths)
    if not corpus:
        return None
    candidate = json.loads(json.dumps(raw, ensure_ascii=False))
    tool = next(
        (
            item
            for service in candidate.get("services", [])
            for item in service.get("tools", [])
            if str(item.get("name")) == tool_name
        ),
        None,
    )
    if not isinstance(tool, dict):
        return None
    smoke = tool.get("smokeTest")
    if not isinstance(smoke, dict) or not isinstance(smoke.get("input"), dict):
        return None
    ungrounded = _ungrounded_smoke_strings(
        smoke["input"],
        tool.get("inputSchema", {}),
        corpus,
    )
    # Values already present in the full corpus may merely cite the wrong file.
    free_text = _ungrounded_smoke_strings(
        smoke["input"],
        tool.get("inputSchema", {}),
        "",
    )
    replacements: dict[str, str] = {}
    provenance: dict[str, str] = {}
    for value in free_text:
        exact = _smoke_candidate_provenance(root, candidate_paths, {value})
        if exact:
            replacements[value] = value
            provenance[value] = exact[value]
            continue
        if value not in ungrounded:
            continue
        suggestions = _smoke_string_candidates(corpus, value)
        suggested_provenance = _smoke_candidate_provenance(
            root,
            candidate_paths,
            set(suggestions),
        )
        replacement = next(
            (item for item in suggestions if item in suggested_provenance),
            None,
        )
        if replacement is None:
            return None
        replacements[value] = replacement
        provenance[replacement] = suggested_provenance[replacement]
    if not replacements:
        return None
    smoke["input"] = _replace_exact_string_values(
        smoke["input"],
        replacements,
    )
    smoke["evidence"] = sorted(set(provenance.values()))
    return candidate


def _replace_exact_string_values(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_exact_string_values(child, replacements)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_exact_string_values(child, replacements)
            for child in value
        ]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def _augment_unknown_symbol_errors(
    errors: list[str],
    known_symbols: set[str],
) -> list[str]:
    known = sorted(known_symbols)
    augmented: list[str] = []
    for error in errors:
        match = re.search(r"包含未知符号:\s*(.+)$", error)
        if match is None:
            augmented.append(error)
            continue
        suggestions: list[str] = []
        for unknown in (item.strip() for item in match.group(1).split(",")):
            if not unknown:
                continue
            candidates: list[str] = []
            parent = unknown.rpartition(".")[0]
            if parent in known_symbols:
                candidates.append(parent)
            candidates.extend(
                symbol
                for symbol in known
                if symbol.rsplit(".", 1)[-1] == unknown.rsplit(".", 1)[-1]
            )
            candidates.extend(
                difflib.get_close_matches(unknown, known, n=5, cutoff=0.45)
            )
            unique = list(dict.fromkeys(candidates))[:5]
            if unique:
                suggestions.append(f"{unknown} -> {unique}")
        augmented.append(
            error
            + (
                "；可用仓库符号候选（必须核对源码后选择）: "
                + "; ".join(suggestions)
                if suggestions
                else ""
            )
        )
    return augmented


def _canonicalize_nonsemantic_shape(raw: dict[str, Any]) -> None:
    """Repair provider formatting quirks without changing service semantics."""
    services = raw.get("services")
    if not isinstance(services, list):
        return
    raw.setdefault("schemaVersion", SCHEMA_VERSION)
    if raw.get("decision") not in {"package", "reject"}:
        if services:
            raw["decision"] = "package"
        elif raw.get("rejectionReasons"):
            raw["decision"] = "reject"
    if not isinstance(raw.get("analysisSummary"), str) or len(raw["analysisSummary"].strip()) < 10:
        descriptions = [
            str(service.get("description", "")).strip()
            for service in services
            if isinstance(service, dict) and service.get("description")
        ]
        raw["analysisSummary"] = (
            "；".join(descriptions) or "根据仓库源码证据规划可远程调用的算法服务能力。"
        )
    raw.setdefault("excludedSymbols", [])
    raw.setdefault("assumptions", [])
    raw.setdefault("riskNotes", [])
    nested_exclusions: list[Any] = []
    for service in services:
        if not isinstance(service, dict):
            continue
        service_exclusions = service.get("excludedSymbols")
        if isinstance(service_exclusions, list):
            # excludedSymbols audits the whole repository, not one logical service.
            # Some providers nevertheless place it next to service tools. Moving a
            # well-formed list is a structural repair and does not alter semantics.
            nested_exclusions.extend(service.pop("excludedSymbols"))
        service_id = service.get("id")
        if (not isinstance(service_id, str) or not service_id.strip()) and isinstance(
            service.get("name"), str
        ):
            service["id"] = _snake_identifier(service["name"], fallback="algorithm_service")
        elif isinstance(service_id, str):
            service["id"] = _snake_identifier(service_id, fallback="algorithm_service")
        service_name = str(service.get("name") or service.get("description") or "Algorithm service")
        service.setdefault("name", service_name)
        service.setdefault("description", service_name)
        service.setdefault(
            "rationale",
            "These tools share the same algorithm contract, runtime dependencies, and lifecycle.",
        )
        tools = service.get("tools")
        if not isinstance(tools, list):
            continue
        service_symbols = service.pop("sourceSymbols", None)
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            legacy_strategy = tool.pop("adaptationStrategy", None)
            if (
                not isinstance(tool.get("adapterStrategy"), str)
                or not tool["adapterStrategy"].strip()
            ) and isinstance(legacy_strategy, str) and legacy_strategy.strip():
                tool["adapterStrategy"] = legacy_strategy
            if (
                not tool.get("sourceSymbols")
                and isinstance(service_symbols, list)
                and all(isinstance(symbol, str) for symbol in service_symbols)
            ):
                tool["sourceSymbols"] = list(service_symbols)
            if isinstance(tool.get("evidence"), str):
                tool["evidence"] = [tool["evidence"]]
            if (
                not isinstance(tool.get("evidence"), list)
                or not any(isinstance(item, str) and item.strip() for item in tool["evidence"])
            ) and isinstance(tool.get("sourceSymbols"), list):
                tool["evidence"] = list(tool["sourceSymbols"])
            tool.setdefault("dependsOn", [])
            if not isinstance(tool.get("adapterStrategy"), str) or not tool["adapterStrategy"].strip():
                symbols = ", ".join(tool.get("sourceSymbols", [])) or "the audited source capability"
                tool["adapterStrategy"] = (
                    f"Validate the public JSON inputs, call {symbols}, and serialize its result."
                )
            smoke = tool.get("smokeTest")
            if isinstance(smoke, dict) and isinstance(smoke.get("evidence"), str):
                smoke["evidence"] = [smoke["evidence"]]

    if nested_exclusions:
        root_exclusions = raw.get("excludedSymbols", [])
        if isinstance(root_exclusions, list):
            raw["excludedSymbols"] = _merge_excluded_symbols(
                root_exclusions,
                nested_exclusions,
            )


def _independent_smoke_evidence_errors(
    plan: PackagingPlan,
    known_files: set[str],
    *,
    evidence_root: Path | None = None,
) -> list[str]:
    generated_files = {
        "main.py",
        "README.ioeb.md",
        "template_adaptation.json",
    }
    independent_files = known_files - generated_files
    errors: list[str] = []
    for tool in plan.tools:
        smoke = tool.get("smokeTest", {})
        if not smoke.get("enabled"):
            errors.append(
                f"{tool.get('name', '<unnamed>')}.smokeTest.enabled=false；"
                "生产封装要求每个 MCP Tool 都有原仓库测试、doctest 或示例支持的"
                "可执行 smokeTest。请为该工具选择真实 fixture 并设 enabled=true；"
                "若仓库完全没有可执行证据，则该能力不能进入本次生产封装"
            )
            continue
        evidence = smoke.get("evidence", [])
        cited_files = {
            path
            for path in independent_files
            if any(isinstance(item, str) and path in item for item in evidence)
        }
        if not cited_files:
            provenance_hint = ""
            if evidence_root is not None:
                candidate_paths = _smoke_candidate_files(independent_files)
                free_text_values = _ungrounded_smoke_strings(
                    smoke.get("input", {}),
                    tool.get("inputSchema", {}),
                    "",
                )
                exact_provenance = _smoke_candidate_provenance(
                    evidence_root,
                    candidate_paths,
                    set(free_text_values),
                )
                if exact_provenance:
                    provenance_hint = (
                        "；当前 input 已在以下独立文件中逐字出现，必须保持 input 不变并将 "
                        "smokeTest.evidence 改为对应 file:line: "
                        + ", ".join(
                            f"{value[:80]!r} -> {exact_provenance[value]}"
                            for value in free_text_values
                            if value in exact_provenance
                        )
                    )
            errors.append(
                "[smoke_evidence_reference] "
                f"{tool.get('name', '<unnamed>')}.smokeTest.evidence "
                "只引用了生成的 main.py/README.ioeb.md/template_adaptation.json；"
                "请从原仓库可执行测试、doctest 或示例核对输入；"
                "找不到独立可执行证据时，该能力不能进入本次生产封装"
                + provenance_hint
            )
            continue
        if evidence_root is None:
            continue
        corpus = _read_smoke_evidence_corpus(evidence_root, cited_files)
        suggestion_corpus = corpus + "\n" + _read_smoke_evidence_corpus(
            evidence_root,
            _smoke_candidate_files(independent_files),
        )
        ungrounded = _ungrounded_smoke_strings(
            smoke.get("input", {}),
            tool.get("inputSchema", {}),
            corpus,
        )
        if ungrounded:
            rendered = ", ".join(repr(value[:120]) for value in ungrounded[:5])
            candidate_paths = _smoke_candidate_files(independent_files)
            suggestions = {
                value: _smoke_string_candidates(suggestion_corpus, value)
                for value in ungrounded[:5]
            }
            provenance = _smoke_candidate_provenance(
                evidence_root,
                candidate_paths,
                {
                    candidate
                    for candidates in suggestions.values()
                    for candidate in candidates
                },
            )
            rendered_suggestions = "; ".join(
                f"{value[:80]!r} -> ["
                + ", ".join(
                    repr(candidate)
                    + (
                        f" (evidence: {provenance[candidate]})"
                        if candidate in provenance
                        else ""
                    )
                    for candidate in candidates
                )
                + "]"
                for value, candidates in suggestions.items()
                if candidates
            )
            all_values_exist_elsewhere = all(
                value in provenance for value in ungrounded[:5]
            )
            errors.append(
                (
                    "[smoke_evidence_reference] "
                    if all_values_exist_elsewhere
                    else "[smoke_fixture_grounding] "
                )
                + f"{tool.get('name', '<unnamed>')}.smokeTest.input 包含未在所引测试/"
                f"doctest/示例中出现的自由文本值: {rendered}；"
                "必须改用被引用文件中的真实可执行 fixture，不能依据模板注释编造"
                + (
                    "；当前 input 已在候选独立证据中逐字出现，必须保持 input 不变并仅更新 evidence"
                    if all_values_exist_elsewhere
                    else ""
                )
                + (
                    "；仓库测试/doctest/示例中的接近字符串候选"
                    "（使用时必须同步更新 evidence 并核对调用上下文）: "
                    + rendered_suggestions
                    if rendered_suggestions
                    else ""
                )
            )
    return errors


def _smoke_errors_prove_fixture_grounding(errors: list[str] | None) -> bool:
    """Only freeze fixtures whose exact values were found in independent evidence."""

    return bool(errors) and all(
        error.startswith("[smoke_evidence_reference]")
        and "必须保持 input 不变" in error
        for error in errors
    )


def _smoke_candidate_provenance(
    root: Path,
    paths: set[str],
    candidates: set[str],
) -> dict[str, str]:
    if not candidates:
        return {}
    resolved_root = root.resolve()
    remaining = set(candidates)
    result: dict[str, str] = {}
    for relative in sorted(paths):
        if not remaining:
            break
        try:
            path = _contained_path(resolved_root, relative)
        except ValueError:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for candidate in list(remaining):
            offset = text.find(candidate)
            if offset < 0:
                continue
            result[candidate] = f"{relative}:{text.count(chr(10), 0, offset) + 1}"
            remaining.remove(candidate)
    return result


def _smoke_candidate_files(paths: set[str]) -> set[str]:
    candidates: set[str] = set()
    for path in paths:
        lowered = path.lower()
        parts = Path(lowered).parts
        if Path(lowered).suffix not in {".py", ".md", ".rst", ".ipynb"}:
            continue
        if (
            Path(lowered).name.startswith("test")
            or any(
                part in {"test", "tests", "example", "examples", "demo", "demos", "docs"}
                for part in parts
            )
        ):
            candidates.add(path)
    return candidates


def _read_smoke_evidence_corpus(root: Path, paths: set[str]) -> str:
    resolved_root = root.resolve()
    chunks: list[str] = []
    remaining = 1_000_000
    for relative in sorted(paths):
        if remaining <= 0:
            break
        try:
            path = _contained_path(resolved_root, relative)
        except ValueError:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:remaining]
        except OSError:
            continue
        chunks.append(text)
        remaining -= len(text)
    return "\n".join(chunks)


def _ungrounded_smoke_strings(value: Any, schema: Any, corpus: str) -> list[str]:
    schema = schema if isinstance(schema, dict) else {}
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        result: list[str] = []
        for name, child in value.items():
            result.extend(
                _ungrounded_smoke_strings(child, properties.get(name, {}), corpus)
            )
        return result
    if isinstance(value, list):
        result: list[str] = []
        item_schema = schema.get("items", {})
        for child in value:
            result.extend(_ungrounded_smoke_strings(child, item_schema, corpus))
        return result
    if not isinstance(value, str) or len(value.strip()) < 4:
        return []
    if (
        value in schema.get("enum", [])
        or schema.get("const") == value
        or any(
            key in schema
            for key in ("pattern", "format", "contentEncoding", "contentMediaType")
        )
    ):
        return []
    normalized_value = " ".join(value.split())
    normalized_corpus = " ".join(corpus.split())
    return [] if normalized_value in normalized_corpus else [value]


def _smoke_string_candidates(corpus: str, target: str) -> list[str]:
    literals: list[str] = []
    try:
        token_stream = tokenize.generate_tokens(io.StringIO(corpus).readline)
        for token in token_stream:
            if token.type != tokenize.STRING:
                continue
            try:
                value = ast.literal_eval(token.string)
            except (SyntaxError, ValueError):
                continue
            if not isinstance(value, str):
                continue
            if 4 <= len(value.strip()) <= 240 and "\n" not in value.strip():
                literals.append(value.strip())
            if len(value) > 240 or "\n" in value:
                for match in re.finditer(
                    r"(?P<quote>['\"])(?P<value>[^'\"\\n]{4,240})(?P=quote)",
                    value,
                ):
                    literals.append(match.group("value").strip())
    except (IndentationError, SyntaxError, tokenize.TokenError):
        pass

    equilibrium_like = "<->" in target or ("=" in target and "->" not in target)
    kinetics_like = "->" in target and "<->" not in target
    ranked: list[tuple[float, str]] = []
    target_tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9+_-]*", target.lower()))
    for candidate in dict.fromkeys(literals):
        if ";" in target and ";" not in candidate:
            continue
        if equilibrium_like and ("=" not in candidate or "->" in candidate):
            continue
        if kinetics_like and "->" not in candidate:
            continue
        candidate_tokens = set(
            re.findall(r"[A-Za-z][A-Za-z0-9+_-]*", candidate.lower())
        )
        overlap = (
            len(target_tokens & candidate_tokens) / len(target_tokens | candidate_tokens)
            if target_tokens or candidate_tokens
            else 0.0
        )
        similarity = difflib.SequenceMatcher(
            None,
            " ".join(target.split()).lower(),
            " ".join(candidate.split()).lower(),
        ).ratio()
        shape_bonus = 0.0
        if ";" in target and ";" in candidate:
            shape_bonus += 0.15
        if re.search(r"\d", target) and re.search(r"\d", candidate):
            shape_bonus += 0.35
        ranked.append((similarity + overlap + shape_bonus, candidate))
    ranked.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return [candidate for _, candidate in ranked[:5]]


def _merge_excluded_symbols(root: list[Any], nested: list[Any]) -> list[Any]:
    """Merge misplaced exclusions deterministically, preferring root reasons."""
    merged: list[Any] = []
    seen_symbols: set[str] = set()
    for item in [*root, *nested]:
        symbol = item.get("symbol") if isinstance(item, dict) else None
        if isinstance(symbol, str):
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
        merged.append(item)
    return merged


def _drop_unknown_exclusions(raw: dict[str, Any], known_symbols: set[str]) -> None:
    """Discard hallucinated audit entries without changing exposed capabilities.

    An exclusion only documents why a real repository symbol is not exposed as a
    tool.  Removing an entry whose symbol does not exist cannot change the service
    surface; malformed entries remain untouched so the normal validator still
    rejects them.
    """
    exclusions = raw.get("excludedSymbols")
    if not isinstance(exclusions, list):
        return
    raw["excludedSymbols"] = [
        item
        for item in exclusions
        if not (
            isinstance(item, dict)
            and isinstance(item.get("symbol"), str)
            and item["symbol"] not in known_symbols
        )
    ]


def _snake_identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = fallback
    return normalized[:64]


def _parse_structured_string(value: str) -> Any | None:
    """Normalize JSON/Python-like composites emitted as strings by some models."""
    text = value.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        pass
    try:
        tokens = []
        replacements = {"true": "True", "false": "False", "null": "None"}
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NAME and token.string in replacements:
                token = tokenize.TokenInfo(
                    token.type,
                    replacements[token.string],
                    token.start,
                    token.end,
                    token.line,
                )
            tokens.append(token)
        return ast.literal_eval(tokenize.untokenize(tokens))
    except (ValueError, SyntaxError, tokenize.TokenError, IndentationError):
        return None


def _agent_writable_path(relative: str) -> bool:
    return relative in {
        "adapters.py",
        "requirements.txt",
        "requirements-cpu.txt",
        "system-packages.txt",
        "README.generated.md",
    } or (
        relative.startswith("tests/") and relative.endswith((".py", ".json", ".md"))
    )


class WriteArtifactFile(Tool):
    name = "write_artifact_file"
    description = (
        "写入 Agent 负责的语义适配实现和受控依赖清单。允许 adapters.py、"
        "requirements.txt、requirements-cpu.txt、system-packages.txt、"
        "README.generated.md 或 tests/ 下文本文件；"
        "server.py 与 Dockerfile 由已审核规划确定性生成。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        artifact_dir: str | Path,
        *,
        max_chars: int = 250_000,
        allow_nonempty_overwrite: bool = True,
    ) -> None:
        self.root = Path(artifact_dir).resolve()
        self.max_chars = max_chars
        self.allow_nonempty_overwrite = allow_nonempty_overwrite

    def lock_nonempty_overwrites(self) -> None:
        """After first generation, require exact patches for existing content."""

        self.allow_nonempty_overwrite = False

    async def execute(self, **kwargs: Any) -> ToolResult:
        relative = str(kwargs.get("path", ""))
        content = str(kwargs.get("content", ""))
        if not _agent_writable_path(relative):
            return ToolResult(error=f"不允许 Agent 写入该路径: {relative}")
        if len(content) > self.max_chars:
            return ToolResult(error=f"文件内容超过限制: {len(content)} > {self.max_chars}")
        validation_error = _validate_agent_dependency_file(relative, content)
        if validation_error:
            return ToolResult(error=validation_error)
        shadowing_error = _source_shadowing_dependency_error(
            self.root,
            relative,
            content,
        )
        if shadowing_error:
            return ToolResult(error=shadowing_error)
        contract_error = _template_contract_dependency_error(
            self.root,
            relative,
            content,
        )
        if contract_error:
            return ToolResult(error=contract_error)
        path = _contained_path(self.root, relative)
        if (
            not self.allow_nonempty_overwrite
            and path.is_file()
            and path.stat().st_size > 0
        ):
            return ToolResult(
                error=(
                    f"{relative} 已包含首轮实现；验收修复必须使用 "
                    "patch_artifact_file 做精确局部替换"
                )
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(output=f"已写入 {relative} ({len(content)} chars)")


class PatchArtifactFile(Tool):
    name = "patch_artifact_file"
    description = (
        "对 Agent 可写产物做一次精确局部替换。old_text 必须非空且在目标文件中恰好出现一次；"
        "修复验收错误时优先使用此工具，避免重写已通过验证的代码和依赖。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {
                "type": "string",
                "description": "目标文件中恰好出现一次的原始连续文本，必须非空",
            },
            "new_text": {
                "type": "string",
                "description": "替换后的文本；可为空以删除 old_text",
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, artifact_dir: str | Path, *, max_chars: int = 250_000) -> None:
        self.root = Path(artifact_dir).resolve()
        self.writer = WriteArtifactFile(self.root, max_chars=max_chars)

    async def execute(self, **kwargs: Any) -> ToolResult:
        relative = str(kwargs.get("path", ""))
        old_text = str(kwargs.get("old_text", ""))
        new_text = str(kwargs.get("new_text", ""))
        if not _agent_writable_path(relative):
            return ToolResult(error=f"不允许 Agent 修改该路径: {relative}")
        if not old_text:
            return ToolResult(error="old_text 不能为空；空文件或整文件初始化请使用 write_artifact_file")
        path = _contained_path(self.root, relative)
        if not path.is_file() or path.is_symlink():
            return ToolResult(error=f"产物文件不存在或不可修改: {relative}")
        content = path.read_text(encoding="utf-8", errors="replace")
        occurrences = content.count(old_text)
        if occurrences != 1:
            return ToolResult(
                error=(
                    f"old_text 必须在 {relative} 中恰好出现一次，"
                    f"当前出现 {occurrences} 次；请扩大上下文后重试"
                )
            )
        return await self.writer.execute(
            path=relative,
            content=content.replace(old_text, new_text, 1),
        )


def _validate_agent_dependency_file(relative: str, content: str) -> str:
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if relative in {"requirements.txt", "requirements-cpu.txt"}:
        if len(lines) > 300:
            return f"{relative} 依赖条目超过 300 个"
        for line in lines:
            if line.startswith("-"):
                return f"{relative} 禁止 pip 命令行选项: {line}"
            try:
                requirement = Requirement(line)
            except InvalidRequirement as exc:
                return f"{relative} 包含无效 PEP 508 依赖 {line!r}: {exc}"
            if requirement.url:
                return f"{relative} 禁止 URL/VCS/本地路径依赖: {line}"
            if (
                relative == "requirements-cpu.txt"
                and requirement.name.lower().replace("_", "-")
                not in {"torch", "torchvision", "torchaudio"}
            ):
                return (
                    "requirements-cpu.txt 只允许 torch、torchvision、torchaudio，"
                    f"其他依赖请写入 requirements.txt: {line}"
                )
        return ""
    if relative == "system-packages.txt":
        if len(lines) > 100:
            return "system-packages.txt 系统包超过 100 个"
        invalid = [
            line
            for line in lines
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?", line)
        ]
        if invalid:
            return (
                "system-packages.txt 只允许 Debian 包名，禁止参数、命令和 URL: "
                + ", ".join(invalid[:10])
            )
    return ""


def _source_shadowing_dependency_error(
    artifact_root: Path,
    relative: str,
    content: str,
) -> str:
    if relative != "requirements.txt":
        return ""
    owned = _source_owned_distributions(artifact_root / "algorithm")
    if not owned:
        return ""
    declared = {
        canonicalize_name(Requirement(line.strip()).name)
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    shadowing = sorted(owned & declared)
    if not shadowing:
        return ""
    return (
        "requirements.txt 不得重新安装由提交仓库提供的纯 Python 包，否则 "
        "site-packages 会覆盖已审核源码: "
        + ", ".join(shadowing)
        + "；请保留其 install_requires 依赖，但移除同名项目自身"
    )


def _template_contract_dependency_error(
    artifact_root: Path,
    relative: str,
    content: str,
) -> str:
    if relative not in {"requirements.txt", "requirements-cpu.txt"}:
        return ""
    contract = _runtime_requirement_contract(artifact_root / "algorithm")
    if not contract:
        return ""
    declared: set[str] = set()
    for requirement_file in ("requirements.txt", "requirements-cpu.txt"):
        text = (
            content
            if requirement_file == relative
            else (
                (artifact_root / requirement_file).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                if (artifact_root / requirement_file).is_file()
                else ""
            )
        )
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                declared.add(canonicalize_name(Requirement(line).name))
            except InvalidRequirement:
                continue
    missing = sorted(set(contract) - declared)
    if not missing:
        return ""
    return (
        "不得删除提交模板声明的运行依赖: "
        + ", ".join(missing)
        + "；可调整兼容版本或新增依赖，但必须保留模板运行契约"
    )


class ReadArtifactFile(Tool):
    name = "read_artifact_file"
    description = "读取当前生成产物中的文本文件，用于定位验收失败和修复。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, artifact_dir: str | Path, *, max_chars: int = 80_000) -> None:
        self.root = Path(artifact_dir).resolve()
        self.max_chars = max_chars

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = _contained_path(self.root, str(kwargs.get("path", "")))
        if not path.is_file() or path.is_symlink():
            return ToolResult(error="产物文件不存在")
        text = path.read_text(encoding="utf-8", errors="replace")
        return ToolResult(output=text[: self.max_chars] + ("\n...(truncated)" if len(text) > self.max_chars else ""))


class VerifyArtifact(Tool):
    name = "verify_artifact"
    description = "运行确定性验收：规划一致性、MCP Tool 注册、参数 Schema、语法、传输端点、依赖和容器文件。"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, artifact_dir: str | Path, plan: PackagingPlan) -> None:
        self.verifier = ArtifactVerifier(artifact_dir, plan)

    async def execute(self, **kwargs: Any) -> ToolResult:
        report = self.verifier.verify()
        output = report.to_json()
        return ToolResult(output=output if report.passed else "验收失败:\n" + output)
