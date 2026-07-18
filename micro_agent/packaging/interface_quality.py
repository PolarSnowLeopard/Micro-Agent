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
    selector_contract_errors = _selector_output_contract_errors(tools)
    errors.extend(selector_contract_errors)
    required_default_errors = _required_parameter_default_errors(tools)
    errors.extend(required_default_errors)
    service_boundary_errors, shared_source_pairs = _service_boundary_errors(plan)
    errors.extend(service_boundary_errors)

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
        "serviceCount": len(plan.data.get("services", [])),
        "crossServiceSharedSourcePairs": shared_source_pairs,
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


def _selector_output_contract_errors(tools: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for tool in tools:
        input_schema = tool.get("inputSchema")
        output_schema = tool.get("outputSchema")
        if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
            continue
        input_properties = input_schema.get("properties")
        output_properties = output_schema.get("properties")
        if not isinstance(input_properties, dict) or not isinstance(output_properties, dict):
            continue
        output_required = set(output_schema.get("required", []))
        for selector_name, raw_selector in input_properties.items():
            selector = raw_selector if isinstance(raw_selector, dict) else {}
            items = selector.get("items")
            choices = items.get("enum") if isinstance(items, dict) else None
            if selector.get("type") != "array" or not isinstance(choices, list):
                continue
            string_choices = {choice for choice in choices if isinstance(choice, str)}
            controlled_fields = string_choices & set(output_properties)
            unstable_required = sorted(controlled_fields & output_required)
            if not unstable_required:
                continue
            errors.append(
                "[interface_quality] "
                f"{tool['name']}.{selector_name} 可选择返回字段，但 outputSchema.required "
                f"仍把这些条件字段声明为必返: {', '.join(unstable_required)}；"
                "请将选择性字段设为非 required，或改为带 additionalProperties 的领域映射，"
                "不能要求适配器伪造未请求结果"
            )
    return errors


def _required_parameter_default_errors(tools: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for tool in tools:
        input_schema = tool.get("inputSchema")
        if not isinstance(input_schema, dict):
            continue
        properties = input_schema.get("properties")
        if not isinstance(properties, dict):
            continue
        required = input_schema.get("required", [])
        if not isinstance(required, list):
            continue
        contradictory = sorted(
            name
            for name in required
            if isinstance(name, str)
            and isinstance(properties.get(name), dict)
            and "default" in properties[name]
        )
        if contradictory:
            errors.append(
                "[interface_quality] "
                f"{tool['name']} 同时把参数声明为 required 和 default: "
                + ", ".join(contradictory)
                + "；这会误导 Agent 和自动调用器。若缺省值确实可用，请从 required "
                "移除参数并让适配器采用该缺省值；否则删除 default"
            )
    return errors


def _service_boundary_errors(plan: PackagingPlan) -> tuple[list[str], int]:
    """Reject service splits contradicted by the plan's own implementation evidence.

    A service is a lifecycle/dependency boundary, not another label for a Tool.
    Tools that share a source entry point necessarily share implementation state
    inside the generated artifact and cannot honestly claim independent service
    boundaries. Multiple boundaries also need distinct, user-readable reasons;
    the canonical fallback text is intentionally insufficient here.
    """

    services = plan.data.get("services", [])
    if not isinstance(services, list) or len(services) <= 1:
        return [], 0

    errors: list[str] = []
    shared_pairs: list[tuple[str, str, list[str]]] = []
    for left_index, left in enumerate(services):
        if not isinstance(left, dict):
            continue
        left_symbols = {
            symbol
            for tool in left.get("tools", [])
            if isinstance(tool, dict)
            for symbol in tool.get("sourceSymbols", [])
            if isinstance(symbol, str)
        }
        for right in services[left_index + 1 :]:
            if not isinstance(right, dict):
                continue
            right_symbols = {
                symbol
                for tool in right.get("tools", [])
                if isinstance(tool, dict)
                for symbol in tool.get("sourceSymbols", [])
                if isinstance(symbol, str)
            }
            shared = sorted(left_symbols & right_symbols)
            if shared:
                shared_pairs.append(
                    (
                        str(left.get("id", "<unnamed>")),
                        str(right.get("id", "<unnamed>")),
                        shared,
                    )
                )

    if shared_pairs:
        rendered = "; ".join(
            f"{left} / {right}: {', '.join(shared)}"
            for left, right, shared in shared_pairs
        )
        errors.append(
            "[interface_quality] 以下跨服务工具共享同一源码入口，说明它们共享实现、"
            "依赖或生命周期，不能按 Tool 机械拆成独立服务: "
            + rendered
            + "；请合并到同一逻辑服务，服务内保留多个可独立选择的 Tool"
        )

    descriptions: list[tuple[str, str, str]] = []
    for service in services:
        if not isinstance(service, dict):
            continue
        service_id = str(service.get("id", "<unnamed>"))
        name = str(service.get("name", "")).strip()
        description = str(service.get("description", "")).strip()
        rationale = str(service.get("rationale", "")).strip()
        descriptions.append((service_id, description, rationale))
        if _normalized_boundary_text(description) == _normalized_boundary_text(name):
            errors.append(
                "[interface_quality] "
                f"逻辑服务 {service_id} 的 description 只是重复服务名称；"
                "必须说明该边界包含哪些内聚能力、共享什么状态或依赖"
            )

    rationale_owners: dict[str, list[str]] = {}
    for service_id, _, rationale in descriptions:
        rationale_owners.setdefault(_normalized_boundary_text(rationale), []).append(service_id)
    duplicated_rationales = [
        owners
        for normalized, owners in rationale_owners.items()
        if normalized and len(owners) > 1
    ]
    if duplicated_rationales:
        errors.append(
            "[interface_quality] 多个逻辑服务使用了完全相同的边界理由: "
            + "; ".join(", ".join(owners) for owners in duplicated_rationales)
            + "；若无法分别说明不同状态、依赖或生命周期，应合并为一个服务"
        )

    return errors, len(shared_pairs)


def _normalized_boundary_text(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


__all__ = ["InterfaceQualityReport", "assess_interface_quality"]
