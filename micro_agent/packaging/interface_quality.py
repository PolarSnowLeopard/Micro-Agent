"""Reference-free quality gate for Agent-facing MCP interface contracts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from micro_agent.packaging.models import PackagingPlan
from micro_agent.packaging.scaffold import _render_tool_docstring


_PAPER_CONSTRAINT_KEYS = {
    "enum",
    "default",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}


@dataclass
class InterfaceQualityReport:
    passed: bool
    metrics: dict[str, float | int]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def assess_interface_quality(
    plan: PackagingPlan,
    *,
    min_goe: float = 0.72,
) -> InterfaceQualityReport:
    """Assess only reference-free, production-relevant interface evidence.

    The gate deliberately does not inspect AMQ-Bench golden tools, task answers,
    or verification scripts. It mirrors the released GoE formula so benchmark
    movement remains interpretable, then adds output-contract checks that the
    paper metric does not enforce.
    """

    tools = plan.tools
    errors: list[str] = []
    warnings: list[str] = []
    published_descriptions = [_render_tool_docstring(tool) for tool in tools]
    non_empty = [description for description in published_descriptions if description.strip()]
    tool_desc_coverage = len(non_empty) / len(tools) if tools else 0.0
    tool_desc_informativeness = (
        min(
            sum(len(description.split()) for description in published_descriptions)
            / (25.0 * len(tools)),
            1.0,
        )
        if tools
        else 0.0
    )
    tool_desc_distinguishability = _minimum_description_distance(non_empty)

    all_parameters: list[tuple[str, str, dict[str, Any]]] = []
    for tool in tools:
        properties = tool.get("inputSchema", {}).get("properties", {})
        if not isinstance(properties, dict):
            continue
        for name, raw_schema in properties.items():
            schema = raw_schema if isinstance(raw_schema, dict) else {}
            all_parameters.append((tool["name"], name, schema))

    parameter_count = len(all_parameters)
    described_parameters = [
        item
        for item in all_parameters
        if isinstance(item[2].get("description"), str) and item[2]["description"].strip()
    ]
    constrained_parameters = [
        item for item in all_parameters if _PAPER_CONSTRAINT_KEYS.intersection(item[2])
    ]
    param_desc_coverage = (
        len(described_parameters) / parameter_count if parameter_count else 0.0
    )
    constraint_richness = (
        len(constrained_parameters) / parameter_count if parameter_count else 0.0
    )
    tss = (
        tool_desc_coverage * 0.40
        + tool_desc_informativeness * 0.30
        + tool_desc_distinguishability * 0.30
    )
    acs = param_desc_coverage * 0.60 + constraint_richness * 0.40
    goe = tss * 0.40 + acs * 0.60

    missing_parameter_descriptions = [
        f"{tool_name}.{parameter_name}"
        for tool_name, parameter_name, schema in all_parameters
        if not isinstance(schema.get("description"), str) or not schema["description"].strip()
    ]
    if missing_parameter_descriptions:
        errors.append(
            "[interface_quality] 以下 MCP 参数缺少 Agent 可见的 description: "
            + ", ".join(missing_parameter_descriptions)
        )

    weak_tool_descriptions = [
        tool["name"]
        for tool in tools
        if len(str(tool.get("description", "")).split()) < 12
    ]
    if weak_tool_descriptions:
        errors.append(
            "[interface_quality] 以下工具的语义描述不足 12 个英文词；"
            "必须说明做什么、何时使用以及与其他工具的区别，可使用中英双语: "
            + ", ".join(weak_tool_descriptions)
        )

    if len(tools) > 1 and tool_desc_distinguishability < 0.40:
        errors.append(
            "[interface_quality] 工具描述过于相似，Agent 难以选择；"
            f"最小 Jaccard distance={tool_desc_distinguishability:.3f}"
        )

    output_description_coverage, weak_outputs = _output_contract_coverage(tools)
    if weak_outputs:
        errors.append(
            "[interface_quality] 以下输出字段缺少语义 description，或输出 object 未声明稳定字段: "
            + ", ".join(weak_outputs)
        )
    dispatcher_envelopes = _dispatcher_envelope_tools(tools)
    if dispatcher_envelopes:
        errors.append(
            "[interface_quality] 以下 Tool 仍把源码分派器的 success/operation/result/error "
            "控制信封直接暴露给调用者: "
            + ", ".join(dispatcher_envelopes)
            + "；MCP Tool 必须只返回该能力的领域成功结果，解包 result、移除固定 operation，"
            "并把失败转换成 MCP error"
        )

    if goe < min_goe:
        errors.append(
            "[interface_quality] 参考 GoE 质量门禁未通过："
            f"{goe:.4f} < {min_goe:.2f}；"
            "请补充参数描述、真实约束并提高工具描述区分度"
        )
    if parameter_count and constraint_richness < 0.30:
        warnings.append(
            "[interface_quality] 少于 30% 的参数包含 enum/default/format/range；"
            "只能依据源码或文档补充真实约束，禁止为了评分编造"
        )

    metrics: dict[str, float | int] = {
        "toolCount": len(tools),
        "parameterCount": parameter_count,
        "toolDescriptionCoverage": round(tool_desc_coverage, 4),
        "toolDescriptionInformativeness": round(tool_desc_informativeness, 4),
        "toolDescriptionDistinguishability": round(tool_desc_distinguishability, 4),
        "parameterDescriptionCoverage": round(param_desc_coverage, 4),
        "constraintRichness": round(constraint_richness, 4),
        "toolSelectionSupport": round(tss, 4),
        "argumentConstructionSupport": round(acs, 4),
        "referenceFreeGoE": round(goe, 4),
        "outputDescriptionCoverage": round(output_description_coverage, 4),
    }
    return InterfaceQualityReport(
        passed=not errors,
        metrics=metrics,
        errors=errors,
        warnings=warnings,
    )


def _minimum_description_distance(descriptions: list[str]) -> float:
    if len(descriptions) <= 1:
        return 1.0
    tokenized = [set(description.lower().split()) for description in descriptions]
    minimum = 1.0
    for index, left in enumerate(tokenized):
        for right in tokenized[index + 1 :]:
            union = len(left | right)
            intersection = len(left & right)
            minimum = min(minimum, 1.0 - (intersection / union if union else 1.0))
    return minimum


def _output_contract_coverage(
    tools: list[dict[str, Any]],
) -> tuple[float, list[str]]:
    total = 0
    described = 0
    weak: list[str] = []
    for tool in tools:
        schema = tool.get("outputSchema")
        if not isinstance(schema, dict):
            weak.append(f"{tool['name']}.outputSchema")
            continue
        schema_description = schema.get("description")
        if isinstance(schema_description, str) and schema_description.strip():
            total += 1
            described += 1
            continue
        if schema.get("type") == "object":
            properties = schema.get("properties")
            if not isinstance(properties, dict) or not properties:
                weak.append(f"{tool['name']}.outputSchema")
                continue
            for name, raw_child in properties.items():
                total += 1
                child = raw_child if isinstance(raw_child, dict) else {}
                description = child.get("description")
                if isinstance(description, str) and description.strip():
                    described += 1
                else:
                    weak.append(f"{tool['name']}.outputSchema.{name}")
            continue
        total += 1
        weak.append(f"{tool['name']}.outputSchema")
    return (described / total if total else 0.0), weak


def _dispatcher_envelope_tools(tools: list[dict[str, Any]]) -> list[str]:
    offenders: list[str] = []
    for tool in tools:
        schema = tool.get("outputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        names = set(properties)
        has_status_envelope = {"success", "result", "error"} <= names
        has_dispatch_envelope = {"operation", "result"} <= names
        if has_status_envelope or has_dispatch_envelope:
            offenders.append(str(tool.get("name", "<unnamed>")))
    return offenders


__all__ = ["InterfaceQualityReport", "assess_interface_quality"]
