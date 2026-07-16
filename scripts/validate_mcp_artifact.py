#!/usr/bin/env python3
"""Validate a running generated artifact through the public MCP protocol."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate
from mcp import ClientSession
from mcp.client.sse import sse_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/sse")
    parser.add_argument("--plan", type=Path, default=Path("packaging_plan.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    planned_tools = [
        tool
        for service in plan.get("services", [])
        for tool in service.get("tools", [])
    ]
    report: dict[str, Any] = {
        "url": args.url,
        "plannedTools": [tool["name"] for tool in planned_tools],
        "discoveredTools": [],
        "toolDiscoveryExact": False,
        "smokeTests": [],
        "securityTests": [],
        "passed": False,
    }
    try:
        async with sse_client(args.url, timeout=args.timeout) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = await session.list_tools()
                report["discoveredTools"] = [tool.name for tool in listed.tools]
                report["toolDiscoveryExact"] = set(report["discoveredTools"]) == set(report["plannedTools"])
                for tool in planned_tools:
                    smoke = tool.get("smokeTest", {})
                    if not smoke.get("enabled"):
                        continue
                    started = time.perf_counter()
                    case = {
                        "tool": tool["name"],
                        "passed": False,
                        "latencyMs": 0.0,
                        "error": None,
                    }
                    try:
                        result = await session.call_tool(tool["name"], smoke.get("input", {}))
                        case["latencyMs"] = round((time.perf_counter() - started) * 1000, 3)
                        if result.isError:
                            case["error"] = _content_text(result)
                        else:
                            value = _structured_value(result, tool.get("outputSchema", {}))
                            validate(instance=value, schema=tool.get("outputSchema", {}))
                            case["passed"] = True
                            case["outputPreview"] = _preview(value)
                    except (Exception, ValidationError) as exc:
                        case["latencyMs"] = round((time.perf_counter() - started) * 1000, 3)
                        case["error"] = f"{type(exc).__name__}: {exc}"
                    report["smokeTests"].append(case)
                for tool in planned_tools:
                    for input_name in _zip_input_names(tool):
                        arguments = dict(tool.get("smokeTest", {}).get("input", {}))
                        arguments[input_name] = _unsafe_zip_base64()
                        case = {
                            "tool": tool["name"],
                            "case": "zip_path_traversal_rejected",
                            "passed": False,
                            "error": None,
                        }
                        try:
                            result = await session.call_tool(tool["name"], arguments)
                            case["passed"] = bool(result.isError)
                            if not result.isError:
                                case["error"] = "unsafe ZIP was accepted as a successful MCP result"
                        except Exception as exc:
                            case["passed"] = True
                            case["error"] = f"rejected with {type(exc).__name__}: {exc}"
                        report["securityTests"].append(case)
                    for input_name in _dataset_zip_input_names(tool):
                        arguments = dict(tool.get("smokeTest", {}).get("input", {}))
                        arguments[input_name] = _incomplete_dataset_zip_base64()
                        case = {
                            "tool": tool["name"],
                            "case": "incomplete_dataset_zip_rejected",
                            "passed": False,
                            "error": None,
                        }
                        try:
                            result = await session.call_tool(tool["name"], arguments)
                            case["passed"] = bool(result.isError)
                            if not result.isError:
                                case["error"] = (
                                    "incomplete dataset was returned as MCP success: "
                                    + _content_text(result)[:500]
                                )
                        except Exception as exc:
                            case["passed"] = True
                            case["error"] = f"rejected with {type(exc).__name__}: {exc}"
                        report["securityTests"].append(case)
    except Exception as exc:
        report["connectionError"] = f"{type(exc).__name__}: {exc}"

    smoke_count = len(report["smokeTests"])
    smoke_passed = sum(1 for case in report["smokeTests"] if case["passed"])
    report["smokeTestCount"] = smoke_count
    report["smokeTestPassed"] = smoke_passed
    report["smokePassRate"] = round(smoke_passed / smoke_count, 4) if smoke_count else None
    report["securityTestCount"] = len(report["securityTests"])
    report["securityTestPassed"] = sum(1 for case in report["securityTests"] if case["passed"])
    report["passed"] = bool(
        report["toolDiscoveryExact"]
        and smoke_count > 0
        and smoke_passed == smoke_count
        and report["securityTestPassed"] == report["securityTestCount"]
    )
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output, flush=True)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def _structured_value(result: Any, schema: dict[str, Any]) -> Any:
    structured = result.structuredContent
    if structured is not None:
        if schema.get("type") == "object":
            return structured
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    text = _content_text(result)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _content_text(result: Any) -> str:
    return "\n".join(getattr(item, "text", "") for item in result.content if hasattr(item, "text"))


def _preview(value: Any, max_chars: int = 500) -> Any:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) <= max_chars:
        return value
    return rendered[:max_chars] + "...(truncated)"


def _zip_input_names(tool: dict[str, Any]) -> list[str]:
    result = []
    properties = tool.get("inputSchema", {}).get("properties", {})
    if not isinstance(properties, dict):
        return result
    for name, schema in properties.items():
        description = str(schema.get("description", "")) if isinstance(schema, dict) else ""
        combined = f"{name} {description}".lower()
        if "zip" in combined and ("base64" in combined or "file" in combined):
            result.append(name)
    return result


def _unsafe_zip_base64() -> str:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../../escape.txt", "blocked")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _dataset_zip_input_names(tool: dict[str, Any]) -> list[str]:
    return [
        name
        for name in _zip_input_names(tool)
        if "dataset" in name.lower()
        or "数据集" in str(tool.get("inputSchema", {}).get("properties", {}).get(name, {}).get("description", ""))
    ]


def _incomplete_dataset_zip_base64() -> str:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("meta.yaml", "dataset_name: intentionally_incomplete\n")
    return base64.b64encode(stream.getvalue()).decode("ascii")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
