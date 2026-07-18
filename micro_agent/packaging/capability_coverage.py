"""Deterministic coverage checks for branch-dispatched repository capabilities."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from micro_agent.packaging.models import PackagingPlan


def assess_dispatch_coverage(
    plan: PackagingPlan,
    symbol_dispatch_branches: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Require semantically distinct literal branches to remain Agent-visible.

    A repository template often exposes one ``main_process(operation=...)``
    function even though each literal selects a distinct capability with its
    own parameters and output. A generic one-tool wrapper merely moves the
    dispatch problem to the calling Agent. For two or more statically observed
    values, the plan must instead fix one value per Tool in ``adapterStrategy``
    and remove the dispatch parameter from that Tool's public schema.
    """

    errors: list[str] = []
    for symbol, raw_branches in sorted(symbol_dispatch_branches.items()):
        groups: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for branch in raw_branches:
            parameter = branch.get("parameter")
            value = branch.get("value")
            if not isinstance(parameter, str) or not parameter or value is None:
                continue
            key = str(value)
            groups[parameter][key] = branch

        for parameter, by_value in sorted(groups.items()):
            if len(by_value) < 2:
                continue
            relevant_tools = [
                tool for tool in plan.tools if symbol in tool.get("sourceSymbols", [])
            ]
            if not relevant_tools:
                continue
            exposed = [
                tool["name"]
                for tool in relevant_tools
                if parameter
                in tool.get("inputSchema", {}).get("properties", {})
            ]
            if exposed:
                errors.append(
                    f"[dispatch_coverage] {symbol} 通过 {parameter} 分派 "
                    f"{len(by_value)} 个静态能力分支；以下 Tool 仍把分派参数暴露给调用 Agent: "
                    + ", ".join(exposed)
                )

            assigned: dict[str, list[str]] = defaultdict(list)
            uncovered: list[str] = []
            for value in sorted(by_value):
                matches = [
                    tool["name"]
                    for tool in relevant_tools
                    if _strategy_fixes_value(
                        str(tool.get("adapterStrategy", "")), parameter, value
                    )
                ]
                if not matches:
                    uncovered.append(value)
                for name in matches:
                    assigned[name].append(value)
            if uncovered:
                errors.append(
                    f"[dispatch_coverage] {symbol}.{parameter} 的静态分支未被独立 Tool 覆盖: "
                    + ", ".join(uncovered)
                    + "；每个 Tool 的 adapterStrategy 必须明确固定一个分支值"
                )
            ambiguous = {
                name: values for name, values in assigned.items() if len(values) > 1
            }
            if ambiguous:
                detail = "; ".join(
                    f"{name} -> {', '.join(values)}"
                    for name, values in sorted(ambiguous.items())
                )
                errors.append(
                    f"[dispatch_coverage] 一个 Tool 不能同时代表 {parameter} 的多个语义分支: "
                    + detail
                )
    return errors


def _strategy_fixes_value(strategy: str, parameter: str, value: str) -> bool:
    normalized = " ".join(strategy.lower().split())
    parameter_pattern = re.escape(parameter.lower())
    value_pattern = re.escape(value.lower())
    patterns = (
        rf"\b{parameter_pattern}\b\s*=\s*['\"]?{value_pattern}['\"]?",
        rf"\bfix(?:es|ed)?\s+\b{parameter_pattern}\b\s+(?:to|as)\s+['\"]?{value_pattern}['\"]?",
        rf"\bset(?:s)?\s+\b{parameter_pattern}\b\s+(?:to|as)\s+['\"]?{value_pattern}['\"]?",
        rf"\binject(?:s)?\s+\b{parameter_pattern}\b\s+(?:as|with|to)\s+['\"]?{value_pattern}['\"]?",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


__all__ = ["assess_dispatch_coverage"]
