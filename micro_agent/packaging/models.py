"""Validated contract shared by the planning Agent, builder Agent and UI graph."""

from __future__ import annotations

import ast
import copy
import base64
import binascii
import io
import json
import keyword
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable


SCHEMA_VERSION = "ioeb.agentic-mcp-plan/v1"
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class PlanValidationError(ValueError):
    """Raised when an Agent plan cannot be trusted by downstream stages."""

    def __init__(self, errors: Iterable[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class PackagingPlan:
    """A validated, immutable-by-convention packaging plan."""

    data: dict[str, Any]

    @classmethod
    def validate(
        cls,
        raw: dict[str, Any],
        *,
        known_symbols: set[str] | None = None,
        known_files: set[str] | None = None,
        symbol_required_parameters: dict[str, list[str]] | None = None,
        symbol_calls: dict[str, list[str]] | None = None,
        symbol_is_generator: dict[str, bool] | None = None,
        candidate_symbols: set[str] | None = None,
    ) -> "PackagingPlan":
        if not isinstance(raw, dict):
            raise PlanValidationError(["plan 必须是 JSON object"])

        data = copy.deepcopy(raw)
        errors: list[str] = []
        if data.get("schemaVersion") != SCHEMA_VERSION:
            errors.append(f"schemaVersion 必须是 {SCHEMA_VERSION}")

        decision = data.get("decision")
        if decision not in {"package", "reject"}:
            errors.append("decision 必须是 package 或 reject")

        summary = data.get("analysisSummary")
        if not isinstance(summary, str) or len(summary.strip()) < 10:
            errors.append("analysisSummary 必须给出不少于 10 个字符的分析结论")

        if decision == "reject":
            reasons = data.get("rejectionReasons")
            if not isinstance(reasons, list) or not any(
                isinstance(item, str) and item.strip() for item in reasons
            ):
                errors.append("拒绝封装时必须提供 rejectionReasons")
            if data.get("services") not in (None, []):
                errors.append("拒绝封装时 services 必须为空")
            if errors:
                raise PlanValidationError(errors)
            data.setdefault("services", [])
            data.setdefault("excludedSymbols", [])
            data.setdefault("assumptions", [])
            data.setdefault("riskNotes", [])
            return cls(data)

        services = data.get("services")
        if not isinstance(services, list) or not 1 <= len(services) <= 5:
            errors.append("services 必须包含 1 到 5 个逻辑服务边界")
            services = []

        seen_service_ids: set[str] = set()
        seen_tool_names: set[str] = set()
        tools: list[dict[str, Any]] = []
        for service_index, service in enumerate(services):
            prefix = f"services[{service_index}]"
            if not isinstance(service, dict):
                errors.append(f"{prefix} 必须是 object")
                continue
            if "excludedSymbols" in service:
                errors.append(
                    f"{prefix}.excludedSymbols 字段层级错误；"
                    "必须移动到规划顶层 excludedSymbols"
                )
            service_id = service.get("id")
            if not isinstance(service_id, str) or not _SNAKE_CASE.match(service_id):
                errors.append(f"{prefix}.id 必须是 snake_case")
            elif service_id in seen_service_ids:
                errors.append(f"逻辑服务 id 重复: {service_id}")
            else:
                seen_service_ids.add(service_id)

            for field in ("name", "description", "rationale"):
                if not isinstance(service.get(field), str) or not service[field].strip():
                    errors.append(f"{prefix}.{field} 不能为空")

            service_tools = service.get("tools")
            if not isinstance(service_tools, list) or not service_tools:
                errors.append(f"{prefix}.tools 至少包含一个工具")
                continue
            tools.extend(item for item in service_tools if isinstance(item, dict))
            if len(service_tools) != len([item for item in service_tools if isinstance(item, dict)]):
                errors.append(f"{prefix}.tools 中存在非 object 条目")

        if len(tools) > 20:
            errors.append("单个 MCP Server 最多规划 20 个工具")

        for tool_index, tool in enumerate(tools):
            prefix = f"tool[{tool_index}]"
            name = tool.get("name")
            if not isinstance(name, str) or not _SNAKE_CASE.match(name):
                errors.append(f"{prefix}.name 必须是 snake_case")
            elif name in seen_tool_names:
                errors.append(f"工具名称重复: {name}")
            else:
                seen_tool_names.add(name)
                if name in {
                    "health", "health_check", "readiness", "liveness", "status",
                    "service_status", "get_model_info", "model_info", "get_model_metadata",
                    "model_metadata", "get_metadata", "metadata",
                }:
                    errors.append(
                        f"{prefix}.name={name} 是运维/模型元数据接口，不是用户算法能力；应写入 excludedSymbols"
                    )

            if not isinstance(tool.get("description"), str) or len(tool["description"].strip()) < 8:
                errors.append(f"{prefix}.description 描述不足")

            symbols = tool.get("sourceSymbols")
            if not isinstance(symbols, list) or not symbols or not all(
                isinstance(symbol, str) and symbol for symbol in symbols
            ):
                errors.append(f"{prefix}.sourceSymbols 至少引用一个源码符号")
            elif known_symbols is not None:
                unknown = sorted(set(symbols) - known_symbols)
                if unknown:
                    errors.append(f"{prefix}.sourceSymbols 包含未知符号: {', '.join(unknown)}")

            for schema_field in ("inputSchema", "outputSchema"):
                schema = tool.get(schema_field)
                if not isinstance(schema, dict) or not isinstance(schema.get("type"), str):
                    errors.append(f"{prefix}.{schema_field} 必须是带 type 的 JSON Schema")
                elif _server_path_fields(schema):
                    errors.append(
                        f"{prefix}.{schema_field} 暴露了容器文件系统字段 "
                        f"{sorted(_server_path_fields(schema))}；远程 MCP 调用者无法访问服务端路径，"
                        "必须在一个端到端工具内完成中间文件处理"
                    )
            input_schema = tool.get("inputSchema")
            if isinstance(input_schema, dict) and input_schema.get("type") != "object":
                errors.append(f"{prefix}.inputSchema.type 必须是 object")
            if isinstance(input_schema, dict):
                properties = input_schema.get("properties", {})
                required = input_schema.get("required", [])
                if not isinstance(properties, dict):
                    errors.append(f"{prefix}.inputSchema.properties 必须是 object")
                    properties = {}
                invalid_params = [
                    name
                    for name in properties
                    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name)
                ]
                if invalid_params:
                    errors.append(f"{prefix}.inputSchema 包含非法参数名: {invalid_params}")
                if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
                    errors.append(f"{prefix}.inputSchema.required 必须是参数名数组")
                elif not set(required).issubset(properties):
                    errors.append(f"{prefix}.inputSchema.required 引用了未定义参数")
                if (
                    symbol_required_parameters is not None
                    and isinstance(symbols, list)
                    and len(symbols) == 1
                    and symbols[0] in symbol_required_parameters
                ):
                    strategy_text = str(
                        tool.get("adapterStrategy") or tool.get("adaptationStrategy") or ""
                    )
                    missing_parameters = _unmapped_source_parameters(
                        symbol_required_parameters[symbols[0]],
                        properties,
                        required if isinstance(required, list) else [],
                        strategy_text,
                        source_symbol=symbols[0],
                    )
                    if missing_parameters:
                        errors.append(
                            f"{prefix}.inputSchema 参数不足以调用 {symbols[0]}："
                            f"源码必填参数为 {symbol_required_parameters[symbols[0]]}；"
                            f"未暴露且未说明派生方式的参数为 {missing_parameters}。"
                            "若参数语义允许缺省，请在 adapterStrategy 中逐项明确写出"
                            + "、".join(f"`{item}=None`" for item in missing_parameters)
                            + "（或真实源码常量）；否则必须把参数加入 inputSchema"
                        )
            output_schema = tool.get("outputSchema")
            if (
                symbol_is_generator is not None
                and isinstance(symbols, list)
                and len(symbols) == 1
                and symbol_is_generator.get(symbols[0])
                and isinstance(output_schema, dict)
                and output_schema.get("type") != "array"
            ):
                errors.append(
                    f"{prefix}.outputSchema 必须是 array：源码 {symbols[0]} 使用 yield 返回多项结果"
                )
            if isinstance(output_schema, dict) and output_schema.get("type") == "object":
                output_properties = output_schema.get("properties", {})
                output_required = output_schema.get("required", [])
                if not isinstance(output_properties, dict):
                    errors.append(f"{prefix}.outputSchema.properties 必须是 object")
                elif output_properties and (
                    not isinstance(output_required, list)
                    or not output_required
                    or not all(isinstance(name, str) for name in output_required)
                ):
                    errors.append(
                        f"{prefix}.outputSchema 声明了字段时必须用 required 明确至少一个稳定返回字段"
                    )
                elif isinstance(output_required, list) and not set(output_required).issubset(output_properties):
                    errors.append(f"{prefix}.outputSchema.required 引用了未定义字段")

            strategy = tool.get("adapterStrategy")
            if not isinstance(strategy, str) or len(strategy.strip()) < 8:
                errors.append(f"{prefix}.adapterStrategy 必须说明适配或重构策略")

            depends_on = tool.get("dependsOn", [])
            if not isinstance(depends_on, list) or not all(isinstance(v, str) for v in depends_on):
                errors.append(f"{prefix}.dependsOn 必须是工具名数组")

            smoke = tool.get("smokeTest")
            if not isinstance(smoke, dict) or not isinstance(smoke.get("enabled"), bool):
                errors.append(f"{prefix}.smokeTest 必须声明 enabled")
            elif smoke.get("enabled") and not isinstance(smoke.get("input"), dict):
                errors.append(f"{prefix}.smokeTest.input 必须是 object")
            elif not smoke.get("enabled") and not isinstance(smoke.get("rationale"), str):
                errors.append(f"{prefix}.smokeTest.enabled=false 时必须说明 rationale")
            elif smoke.get("enabled"):
                smoke_errors = _validate_smoke_input(smoke.get("input", {}), input_schema or {})
                errors.extend(f"{prefix}.smokeTest: {error}" for error in smoke_errors)
                smoke_evidence = smoke.get("evidence")
                if not isinstance(smoke_evidence, list) or not smoke_evidence or not all(
                    isinstance(item, str) and item.strip() for item in smoke_evidence
                ):
                    errors.append(
                        f"{prefix}.smokeTest.evidence 必须指出样例值或约束来自仓库中的哪个文件"
                    )
                elif known_files is not None and not any(
                    any(file_path in item for file_path in known_files)
                    for item in smoke_evidence
                ):
                    errors.append(
                        f"{prefix}.smokeTest.evidence 未引用仓库中的真实文件；无可追溯输入时应 enabled=false"
                    )

            evidence = tool.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence
            ):
                errors.append(f"{prefix}.evidence 至少包含一条源码或文档证据")

        for tool in tools:
            for dependency in tool.get("dependsOn", []):
                if dependency not in seen_tool_names:
                    errors.append(f"工具 {tool.get('name')} 依赖不存在的工具 {dependency}")
                if dependency == tool.get("name"):
                    errors.append(f"工具 {tool.get('name')} 不能依赖自身")

        semantic_surfaces: dict[str, str] = {}
        for tool in tools:
            surface = json.dumps(
                {
                    "sourceSymbols": sorted(tool.get("sourceSymbols", [])),
                    "inputSchema": tool.get("inputSchema"),
                    "outputSchema": tool.get("outputSchema"),
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            previous = semantic_surfaces.get(surface)
            if previous:
                errors.append(
                    f"工具 {previous} 与 {tool.get('name')} 暴露了完全相同的源码和输入输出，"
                    "必须合并为一个端到端用户能力"
                )
            else:
                semantic_surfaces[surface] = str(tool.get("name"))

        if errors:
            raise PlanValidationError(errors)

        data.setdefault("rejectionReasons", [])
        data.setdefault("excludedSymbols", [])
        data.setdefault("assumptions", [])
        data.setdefault("riskNotes", [])
        for field in ("rejectionReasons", "assumptions", "riskNotes"):
            if not isinstance(data[field], list) or not all(isinstance(item, str) for item in data[field]):
                errors.append(f"{field} 必须是字符串数组")
        excluded = data["excludedSymbols"]
        if not isinstance(excluded, list):
            errors.append("excludedSymbols 必须是数组")
        elif not all(
            isinstance(item, dict)
            and isinstance(item.get("symbol"), str)
            and isinstance(item.get("reason"), str)
            for item in excluded
        ):
            errors.append("excludedSymbols 每项必须包含 symbol 和 reason")
        elif candidate_symbols is not None:
            included_symbols = {
                symbol
                for tool in tools
                for symbol in tool.get("sourceSymbols", [])
            }
            excluded_symbols = {item["symbol"] for item in excluded}
            unknown_excluded = sorted(
                excluded_symbols - (known_symbols if known_symbols is not None else candidate_symbols)
            )
            if unknown_excluded:
                errors.append(f"excludedSymbols 包含仓库中不存在的符号: {unknown_excluded}")
            unclassified = sorted(candidate_symbols - included_symbols - excluded_symbols)
            if unclassified:
                errors.append(
                    "以下公开可调用符号尚未说明暴露或排除理由: "
                    + ", ".join(unclassified)
                )
            if symbol_calls is not None:
                composed = _reachable_symbols(included_symbols, symbol_calls)
                unjustified = sorted(
                    symbol
                    for symbol in excluded_symbols & candidate_symbols
                    if _looks_like_user_capability(symbol) and symbol not in composed
                )
                if unjustified:
                    errors.append(
                        "以下独立预测/计算能力不能仅以‘内部/非核心/未来支持’为由排除；"
                        "应规划为 Tool，或由 sourceSymbols 中的端到端能力通过调用图真实组合: "
                        + ", ".join(unjustified)
                    )
        if errors:
            raise PlanValidationError(errors)
        return cls(data)

    @property
    def decision(self) -> str:
        return str(self.data["decision"])

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            tool
            for service in self.data.get("services", [])
            for tool in service.get("tools", [])
        ]

    @property
    def tool_names(self) -> list[str]:
        return [str(tool["name"]) for tool in self.tools]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=indent)

    def to_frontend_graph(self) -> dict[str, Any]:
        """Convert semantic tools to the legacy graph contract without UI changes."""
        nodes: list[dict[str, Any]] = []
        node_ids: dict[str, str] = {}
        for index, tool in enumerate(self.tools):
            node_id = str(9001 + index)
            node_ids[tool["name"]] = node_id
            properties = tool["inputSchema"].get("properties", {})
            required = set(tool["inputSchema"].get("required", []))
            inputs = []
            if isinstance(properties, dict):
                for name, schema in properties.items():
                    type_name = schema.get("type", "any") if isinstance(schema, dict) else "any"
                    suffix = "" if name in required else "?"
                    inputs.append(f"{name}{suffix}: {type_name}")
            nodes.append(
                {
                    "id": node_id,
                    "x": (index % 4) * 180,
                    "y": (index // 4) * 140,
                    "label": tool["name"],
                    "size": 50,
                    "input": ", ".join(inputs) or "None",
                    "output": _schema_label(tool["outputSchema"]),
                    "description": tool["description"],
                    "environment": "Agent 语义规划",
                    "process": tool["adapterStrategy"],
                    "apiType": "mcp",
                    "methodType": "tool",
                    "inputType": "json",
                    "outputType": "json",
                    "mcpType": "tool",
                    "serviceId": _service_id_for_tool(self.data, tool["name"]),
                    "sourceSymbols": tool["sourceSymbols"],
                }
            )

        edges: list[dict[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        for tool in self.tools:
            target = node_ids[tool["name"]]
            for dependency in tool.get("dependsOn", []):
                source = node_ids.get(dependency)
                if source and (source, target) not in seen_edges:
                    edges.append({"sourceID": source, "targetID": target})
                    seen_edges.add((source, target))
        return {
            "nodes": nodes,
            "edges": edges,
            "meta": {
                "schemaVersion": SCHEMA_VERSION,
                "engine": "agentic",
                "serviceCount": len(self.data.get("services", [])),
                "toolCount": len(nodes),
                "analysisSummary": self.data.get("analysisSummary", ""),
            },
        }


def _schema_label(schema: dict[str, Any]) -> str:
    type_name = str(schema.get("type", "any"))
    if type_name == "object" and isinstance(schema.get("properties"), dict):
        keys = ", ".join(schema["properties"].keys())
        return f"object({keys})" if keys else "object"
    if type_name == "array":
        items = schema.get("items", {})
        return f"array[{items.get('type', 'any')}]" if isinstance(items, dict) else "array"
    return type_name


def _service_id_for_tool(data: dict[str, Any], tool_name: str) -> str:
    for service in data.get("services", []):
        if any(tool.get("name") == tool_name for tool in service.get("tools", [])):
            return str(service.get("id", ""))
    return ""


_SERVER_PATH_NAMES = {
    "path", "dir", "directory", "save_dir", "data_path", "dataset_path",
    "file_path", "model_path", "output_path", "input_path", "temp_dir",
}


def _unmapped_source_parameters(
    source_required: list[str],
    properties: dict[str, Any],
    schema_required: list[str],
    strategy: str,
    *,
    source_symbol: str,
) -> list[str]:
    """Require each source argument to be public or explicitly derived.

    Adapter inputs do not need a one-to-one relationship with a lower-level
    function: for example, one uploaded ZIP can derive both ``wsi_dir`` and
    ``output_dir``.  The plan must nevertheless name every internally supplied
    source argument so the builder and reviewer can audit the mapping.
    """
    required_names = set(schema_required)
    missing: list[str] = []
    for index, parameter in enumerate(source_required):
        schema = properties.get(parameter)
        if isinstance(schema, dict) and (
            parameter in required_names or "default" in schema
        ):
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(parameter)}(?![A-Za-z0-9_])", strategy):
            continue
        if _strategy_passes_literal_position(
            strategy,
            function_name=source_symbol.rsplit(".", 1)[-1],
            position=index,
        ):
            continue
        missing.append(parameter)
    return missing


def _strategy_passes_literal_position(
    strategy: str,
    *,
    function_name: str,
    position: int,
) -> bool:
    """Recognize an explicitly fixed positional argument in a strategy.

    A plan such as ``main_process(smiles, 'similarity', options)`` has supplied
    the source ``operation`` parameter even though the prose does not repeat its
    name. Only literal arguments on calls to the exact source function count.
    """
    call_start = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(function_name)}\s*\(")
    for match in call_start.finditer(strategy):
        candidate = _balanced_call(strategy, match.start())
        if not candidate:
            continue
        try:
            expression = ast.parse(candidate, mode="eval").body
        except SyntaxError:
            continue
        if (
            isinstance(expression, ast.Call)
            and len(expression.args) > position
            and isinstance(expression.args[position], ast.Constant)
        ):
            return True
    return False


def _balanced_call(text: str, start: int) -> str:
    open_index = text.find("(", start)
    if open_index < 0:
        return ""
    depth = 0
    quote = ""
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def _server_path_fields(schema: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return found
    for name, child in properties.items():
        normalized = str(name).lower()
        if normalized in _SERVER_PATH_NAMES or normalized.endswith(("_path", "_dir", "_directory")):
            found.add(str(name))
        if isinstance(child, dict):
            found.update(_server_path_fields(child))
            items = child.get("items")
            if isinstance(items, dict):
                found.update(_server_path_fields(items))
    return found


def _validate_smoke_input(smoke_input: dict[str, Any], input_schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict):
        return errors
    unknown = sorted(set(smoke_input) - set(properties))
    if unknown:
        errors.append(f"input 包含 schema 外字段: {unknown}")
    missing = sorted(set(input_schema.get("required", [])) - set(smoke_input))
    if missing:
        errors.append(f"input 缺少必填字段: {missing}")
    for name, value in smoke_input.items():
        child = properties.get(name)
        if not isinstance(child, dict):
            continue
        if value is None and not _schema_allows_null(child):
            errors.append(
                f"字段 {name} 显式传入 null，但其 schema 不允许 null；"
                "若参数可空请声明 nullable 类型，否则从 smoke input 省略该可选字段"
            )
            continue
        if not isinstance(value, str):
            continue
        description = str(child.get("description", "")).lower()
        looks_base64 = "base64" in name.lower() or "base64" in description or child.get("contentEncoding") == "base64"
        if not looks_base64:
            continue
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            errors.append(f"字段 {name} 不是合法 Base64")
            continue
        if "zip" in name.lower() or "zip" in description:
            if not zipfile.is_zipfile(io.BytesIO(decoded)):
                errors.append(f"字段 {name} 不是完整有效的 ZIP；没有真实 fixture 时应设置 enabled=false")
    return errors


def _schema_allows_null(schema: dict[str, Any]) -> bool:
    type_name = schema.get("type")
    if type_name == "null" or (
        isinstance(type_name, list) and "null" in type_name
    ):
        return True
    variants = schema.get("anyOf") or schema.get("oneOf")
    return bool(
        isinstance(variants, list)
        and any(
            isinstance(item, dict) and _schema_allows_null(item)
            for item in variants
        )
    )


def _looks_like_user_capability(symbol: str) -> bool:
    name = symbol.rsplit(".", 1)[-1].lower()
    return bool(
        re.search(
            r"(?:^|_)(?:predict|infer|evaluate|calculate|score|dose|simulate|forecast|classify|detect|recommend)(?:_|$)",
            name,
        )
    )


def _reachable_symbols(
    roots: set[str],
    symbol_calls: dict[str, list[str]],
) -> set[str]:
    by_tail: dict[str, set[str]] = {}
    for symbol in symbol_calls:
        by_tail.setdefault(symbol.rsplit(".", 1)[-1], set()).add(symbol)
    edges: dict[str, set[str]] = {}
    for symbol, calls in symbol_calls.items():
        targets: set[str] = set()
        for call in calls:
            targets.update(by_tail.get(call.rsplit(".", 1)[-1], set()))
        edges[symbol] = targets
    visited = set(roots)
    pending = list(roots)
    while pending:
        current = pending.pop()
        for target in edges.get(current, set()):
            if target not in visited:
                visited.add(target)
                pending.append(target)
    return visited - roots


PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "ioeb.agentic-mcp-plan/v1 packaging plan",
    "properties": {
        "schemaVersion": {"type": "string", "const": SCHEMA_VERSION},
        "decision": {"type": "string", "enum": ["package", "reject"]},
        "analysisSummary": {"type": "string"},
        "rejectionReasons": {"type": "array", "items": {"type": "string"}},
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "rationale": {"type": "string"},
                    "tools": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "sourceSymbols": {"type": "array", "items": {"type": "string"}},
                                "inputSchema": {"type": "object"},
                                "outputSchema": {"type": "object"},
                                "adapterStrategy": {"type": "string"},
                                "dependsOn": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "仅填写同一规划中其他 MCP Tool 的 name；不填写服务 id、源码模块、模型或文件名，无工具依赖时为 []",
                                },
                                "smokeTest": {
                                    "type": "object",
                                    "properties": {
                                        "enabled": {"type": "boolean"},
                                        "input": {"type": "object"},
                                        "rationale": {
                                            "type": "string",
                                            "description": "enabled=false 时说明仓库缺少哪种真实 fixture",
                                        },
                                        "evidence": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "enabled=true 时引用真实样例文件，或明确输入约束所在源码文件与行号",
                                        },
                                    },
                                    "required": ["enabled"],
                                },
                                "evidence": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": [
                                "name", "description", "sourceSymbols", "inputSchema",
                                "outputSchema", "adapterStrategy", "dependsOn", "smokeTest", "evidence",
                            ],
                        },
                    },
                },
                "required": ["id", "name", "description", "rationale", "tools"],
                "additionalProperties": False,
            },
        },
        "excludedSymbols": {
            "type": "array",
            "description": "所有未被 sourceSymbols 使用的公开函数/方法及其不暴露理由",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "reason"],
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "riskNotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "schemaVersion", "decision", "analysisSummary", "services", "excludedSymbols",
    ],
    "additionalProperties": False,
}
