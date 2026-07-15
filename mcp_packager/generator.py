"""Deterministic MCP service artifact generation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from mcp_packager.models import ARTIFACT_VERSION, LoadedPackage, PackagingPlan
from mcp_packager.source import IGNORED_NAMES, iter_source_files


MCP_RUNTIME_REQUIREMENT = "mcp>=1.27,<2"


def _runtime_schema_validation_source() -> str:
    return '''def _ioeb_schema_error(value, schema, path):
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        if any(_ioeb_schema_error(value, item, path) is None for item in alternatives):
            return None
        return f"{path} does not match any allowed type"

    if "const" in schema and value != schema["const"]:
        return f"{path} must equal {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} is not one of the allowed values"

    schema_type = schema.get("type")
    type_matches = {
        "null": value is None,
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, (list, tuple)),
        "object": isinstance(value, dict),
    }
    if schema_type in type_matches and not type_matches[schema_type]:
        return f"{path} must be {schema_type}"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be <= {schema['maximum']}"
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return f"{path} must be > {schema['exclusiveMinimum']}"
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            return f"{path} must be < {schema['exclusiveMaximum']}"
        if "multipleOf" in schema:
            quotient = value / schema["multipleOf"]
            if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                return f"{path} must be a multiple of {schema['multipleOf']}"

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} length must be >= {schema['minLength']}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} length must be <= {schema['maxLength']}"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return f"{path} must match pattern {schema['pattern']}"

    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} item count must be >= {schema['minItems']}"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} item count must be <= {schema['maxItems']}"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _ioeb_schema_error(item, item_schema, f"{path}[{index}]")
                if error:
                    return error
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, item_schema in enumerate(prefix_items):
                if index >= len(value) or not isinstance(item_schema, dict):
                    continue
                error = _ioeb_schema_error(value[index], item_schema, f"{path}[{index}]")
                if error:
                    return error
        if schema.get("uniqueItems"):
            normalized = [json.dumps(item, sort_keys=True, default=str) for item in value]
            if len(normalized) != len(set(normalized)):
                return f"{path} items must be unique"

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [name for name in required if name not in value]
        if missing:
            return f"{path} is missing required properties: {', '.join(missing)}"
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            return f"{path} property count must be >= {schema['minProperties']}"
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            return f"{path} property count must be <= {schema['maxProperties']}"
        for name, item in value.items():
            item_schema = properties.get(name)
            if isinstance(item_schema, dict):
                error = _ioeb_schema_error(item, item_schema, f"{path}.{name}")
                if error:
                    return error
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                return f"{path} contains unsupported property {name}"
            if isinstance(additional, dict):
                error = _ioeb_schema_error(item, additional, f"{path}.{name}")
                if error:
                    return error
    return None


def _ioeb_validate_schema(value, schema, path):
    error = _ioeb_schema_error(value, schema, path)
    if error:
        raise ValueError(error)
    return value
'''


def _tool_wrapper_source(plan: PackagingPlan) -> str:
    function = plan.function
    arguments = []
    call_arguments = []
    doc_args = []
    validators = []
    for parameter in function.parameters:
        field_parts = [f"description={parameter.description!r}"]
        field_mapping = {
            "minimum": "ge",
            "maximum": "le",
            "exclusiveMinimum": "gt",
            "exclusiveMaximum": "lt",
            "multipleOf": "multiple_of",
            "minLength": "min_length",
            "maxLength": "max_length",
            "minItems": "min_length",
            "maxItems": "max_length",
            "pattern": "pattern",
        }
        for schema_name, field_name in field_mapping.items():
            if schema_name in parameter.schema:
                field_parts.append(
                    f"{field_name}={parameter.schema[schema_name]!r}"
                )
        validator_name = f"_ioeb_validate_{parameter.name}"
        validators.append(
            f"def {validator_name}(value):\n"
            f"    return _ioeb_validate_schema(value, {parameter.schema!r}, {parameter.name!r})"
        )
        annotation = (
            f"Annotated[{parameter.annotation}, BeforeValidator({validator_name}), "
            f"WithJsonSchema({parameter.schema!r}), Field({', '.join(field_parts)})]"
        )
        declaration = f"{parameter.name}: {annotation}"
        if not parameter.required:
            declaration += f" = {parameter.default!r}"
        arguments.append(declaration)
        call_arguments.append(f"{parameter.name}={parameter.name}")
        doc_args.append(f"        {parameter.name}: {parameter.description or parameter.name}")

    summary = function.description or plan.service_description
    return_description = function.return_description or "Algorithm result."
    async_prefix = "async " if function.is_async else ""
    await_prefix = "await " if function.is_async else ""
    args_block = "\n".join(doc_args)
    signature = ",\n    ".join(arguments)
    call = ", ".join(call_arguments)
    validator_block = "\n\n\n".join(validators)
    if validator_block:
        validator_block += "\n\n\n"
    return f'''{validator_block}{async_prefix}def ioeb_main_process(
    {signature}
) -> {function.return_annotation}:
    """{summary}

    Args:
{args_block}

    Returns:
        {return_description}
    """
    return {await_prefix}main_process({call})
'''


def _server_source(plan: PackagingPlan) -> str:
    service_name = json.dumps(plan.service_name, ensure_ascii=False)
    description = json.dumps(plan.service_description, ensure_ascii=False)
    schema_validation = _runtime_schema_validation_source()
    wrapper = _tool_wrapper_source(plan)
    return f'''"""Generated MCP adapter. Do not add algorithm logic here."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Dict, List, Literal, Mapping, Optional, Sequence, Tuple, Union

from mcp.server.fastmcp import FastMCP
from pydantic import BeforeValidator, Field, WithJsonSchema


ALGORITHM_DIR = Path(__file__).resolve().parent / "algorithm"
os.chdir(ALGORITHM_DIR)
sys.path.insert(0, str(ALGORITHM_DIR))

from main import main_process  # noqa: E402


{schema_validation}


{wrapper}


mcp = FastMCP(
    name={service_name},
    instructions={description},
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8000")),
    stateless_http=True,
    json_response=True,
)
mcp.tool(name="main_process")(ioeb_main_process)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''


def _verification_source() -> str:
    return '''"""Generated in-container MCP protocol and differential verifier."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parent
ALGORITHM_DIR = ROOT / "algorithm"
os.chdir(ALGORITHM_DIR)
sys.path.insert(0, str(ALGORITHM_DIR))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from main import main_process  # noqa: E402


def normalize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def mcp_result_value(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    content = getattr(result, "content", [])
    if not content:
        return None
    text = getattr(content[0], "text", None)
    if text is None:
        return normalize(content)
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text


def mcp_result_text(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\\n".join(parts)


def result_is_error(result: Any) -> bool:
    return bool(
        getattr(result, "isError", False)
        or getattr(result, "is_error", False)
    )


def mismatched_value(schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    return {
        "string": {"invalid": True},
        "integer": "not-an-integer",
        "number": "not-a-number",
        "boolean": "not-a-boolean",
        "array": {"invalid": True},
        "object": ["invalid"],
    }.get(schema_type, {"invalid": True})


def constraint_mismatched_value(
    schema: dict[str, Any], current_value: Any = None
) -> tuple[bool, Any]:
    if "const" in schema:
        value = schema["const"]
        if isinstance(value, str):
            return True, value + "__ioeb_invalid"
        if isinstance(value, bool):
            return True, not value
        if isinstance(value, (int, float)):
            return True, value + 1
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        if all(isinstance(value, str) for value in enum):
            candidate = "__ioeb_invalid_choice__"
            while candidate in enum:
                candidate += "_"
            return True, candidate
        if all(isinstance(value, int) and not isinstance(value, bool) for value in enum):
            return True, max(enum) + 1
    if "minimum" in schema:
        return True, schema["minimum"] - 1
    if "exclusiveMinimum" in schema:
        return True, schema["exclusiveMinimum"]
    if "maximum" in schema:
        return True, schema["maximum"] + 1
    if "exclusiveMaximum" in schema:
        return True, schema["exclusiveMaximum"]
    if schema.get("minLength", 0) > 0:
        return True, ""
    if "maxLength" in schema:
        return True, "x" * (schema["maxLength"] + 1)
    if schema.get("minItems", 0) > 0:
        return True, []
    if "maxItems" in schema:
        return True, [None] * (schema["maxItems"] + 1)
    if schema.get("uniqueItems") and isinstance(current_value, list) and current_value:
        return True, [current_value[0], current_value[0]]
    if isinstance(current_value, list) and current_value:
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            handled, invalid = constraint_mismatched_value(
                item_schema, current_value[0]
            )
            if handled:
                candidate = list(current_value)
                candidate[0] = invalid
                return True, candidate
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, item_schema in enumerate(prefix_items):
                if index >= len(current_value) or not isinstance(item_schema, dict):
                    continue
                handled, invalid = constraint_mismatched_value(
                    item_schema, current_value[index]
                )
                if handled:
                    candidate = list(current_value)
                    candidate[index] = invalid
                    return True, candidate
    if isinstance(current_value, dict):
        required = schema.get("required")
        if isinstance(required, list) and required:
            candidate = dict(current_value)
            candidate.pop(required[0], None)
            return True, candidate
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, item_schema in properties.items():
                if name not in current_value or not isinstance(item_schema, dict):
                    continue
                handled, invalid = constraint_mismatched_value(
                    item_schema, current_value[name]
                )
                if handled:
                    candidate = dict(current_value)
                    candidate[name] = invalid
                    return True, candidate
        if schema.get("additionalProperties") is False:
            candidate = dict(current_value)
            candidate["__ioeb_unexpected_property__"] = True
            return True, candidate
    return False, None


def schema_contains(actual: Any, expected: Any) -> bool:
    """Return whether an SDK schema contains every generated-plan constraint."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and schema_contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and actual == expected
    return actual == expected


def exception_record(exc: BaseException) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    nested = getattr(exc, "exceptions", None)
    if nested:
        record["causes"] = [exception_record(item) for item in nested]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        record["cause"] = exception_record(cause)
    return record


async def direct_call(arguments: dict[str, Any]) -> Any:
    result = main_process(**arguments)
    if inspect.isawaitable(result):
        result = await result
    return normalize(result)


async def main() -> int:
    artifact = json.loads((ROOT / "ioeb-service.json").read_text(encoding="utf-8"))
    cases = artifact.get("tests", [])
    report: dict[str, Any] = {
        "verificationVersion": "ioeb.mcp-runtime-verification/v1",
        "success": False,
        "endpoint": "http://127.0.0.1:8000/mcp",
        "tool": "main_process",
        "checks": [],
        "cases": [],
        "probes": [],
    }
    try:
        async with streamable_http_client(report["endpoint"]) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                report["checks"].append({"name": "initialize", "success": True})
                tools_result = await session.list_tools()
                tools = {tool.name: tool for tool in tools_result.tools}
                if "main_process" not in tools:
                    raise RuntimeError("tools/list did not expose main_process")
                tool = tools["main_process"]
                expected_tool = artifact["tool"]
                actual_input_schema = normalize(tool.inputSchema)
                if not schema_contains(actual_input_schema, expected_tool["inputSchema"]):
                    raise RuntimeError(
                        f"tools/list input schema differs from plan: {actual_input_schema}"
                    )
                actual_output_schema = normalize(getattr(tool, "outputSchema", None))
                if actual_output_schema is None:
                    actual_output_schema = normalize(getattr(tool, "output_schema", None))
                if actual_output_schema is not None and not schema_contains(
                    actual_output_schema, expected_tool["outputSchema"]
                ):
                    raise RuntimeError(
                        f"tools/list output schema differs from plan: {actual_output_schema}"
                    )
                tool_record = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": actual_input_schema,
                    "outputSchema": actual_output_schema,
                }
                report["tools"] = [tool_record]
                report["checks"].append(
                    {
                        "name": "tools/list",
                        "success": True,
                        "tools": sorted(tools),
                        "toolName": tool_record["name"],
                        "description": tool_record["description"],
                        "inputSchema": tool_record["inputSchema"],
                        "outputSchema": tool_record["outputSchema"],
                    }
                )
                for index, case in enumerate(cases):
                    arguments = case["arguments"]
                    direct = await direct_call(arguments)
                    call_result = await session.call_tool("main_process", arguments=arguments)
                    if getattr(call_result, "isError", False):
                        raise RuntimeError(f"MCP tool returned an error: {call_result.content}")
                    through_mcp = normalize(mcp_result_value(call_result))
                    expected = normalize(case.get("expected"))
                    differential_match = through_mcp == direct
                    expected_match = "expected" not in case or direct == expected
                    case_result = {
                        "name": case.get("name", f"case-{index + 1}"),
                        "success": differential_match and expected_match,
                        "direct": direct,
                        "mcp": through_mcp,
                        "expected": expected if "expected" in case else None,
                        "differentialMatch": differential_match,
                        "expectedMatch": expected_match,
                    }
                    report["cases"].append(case_result)
                if cases:
                    base_arguments = dict(cases[0]["arguments"])
                    required = actual_input_schema.get("required", [])
                    properties = actual_input_schema.get("properties", {})
                    invalid_inputs = []
                    if required:
                        missing_name = required[0]
                        missing_arguments = dict(base_arguments)
                        missing_arguments.pop(missing_name, None)
                        invalid_inputs.append(
                            ("missing-required", missing_name, missing_arguments)
                        )
                    if properties:
                        mismatch_name = next(iter(properties))
                        mismatch_arguments = dict(base_arguments)
                        mismatch_arguments[mismatch_name] = mismatched_value(
                            properties[mismatch_name]
                        )
                        invalid_inputs.append(
                            ("type-mismatch", mismatch_name, mismatch_arguments)
                        )
                    for constraint_name, constraint_schema in properties.items():
                        has_mismatch, mismatch_value = constraint_mismatched_value(
                            constraint_schema, base_arguments.get(constraint_name)
                        )
                        if not has_mismatch:
                            continue
                        constraint_arguments = dict(base_arguments)
                        constraint_arguments[constraint_name] = mismatch_value
                        invalid_inputs.append(
                            (
                                "constraint-violation",
                                constraint_name,
                                constraint_arguments,
                            )
                        )
                    for probe_name, parameter_name, arguments in invalid_inputs:
                        try:
                            probe_result = await session.call_tool(
                                "main_process", arguments=arguments
                            )
                            message = mcp_result_text(probe_result)
                            handled = result_is_error(probe_result)
                            report["probes"].append(
                                {
                                    "name": probe_name,
                                    "parameter": parameter_name,
                                    "handled": handled,
                                    "specific": parameter_name.lower() in message.lower(),
                                    "message": message,
                                }
                            )
                        except Exception as exc:
                            message = str(exc)
                            report["probes"].append(
                                {
                                    "name": probe_name,
                                    "parameter": parameter_name,
                                    "handled": True,
                                    "specific": parameter_name.lower() in message.lower(),
                                    "message": message,
                                }
                            )
        case_success = all(item["success"] for item in report["cases"])
        probe_success = all(
            item.get("handled") and item.get("specific")
            for item in report["probes"]
        )
        report["checks"].append(
            {
                "name": "invalid-input-probes",
                "success": probe_success,
                "passed": sum(
                    bool(item.get("handled")) and bool(item.get("specific"))
                    for item in report["probes"]
                ),
                "total": len(report["probes"]),
            }
        )
        report["success"] = case_success and probe_success
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["success"] else 2
    except Exception as exc:
        report["error"] = exception_record(exc)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
'''


def _dockerfile_source() -> str:
    return '''FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 ioeb \\
    && useradd --system --uid 10001 --gid ioeb --home-dir /nonexistent --shell /usr/sbin/nologin ioeb

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \\
    && python -m pip install -r requirements.txt

COPY --chown=ioeb:ioeb . .

USER 10001:10001
EXPOSE 8000

CMD ["python", "/app/server.py"]
'''


def _compose_source(plan: PackagingPlan) -> str:
    return f'''services:
  mcp-service:
    build:
      context: .
    image: ioeb/{plan.service_name}:${{MCP_IMAGE_TAG:-local}}
    restart: unless-stopped
    ports:
      - "127.0.0.1:${{MCP_HOST_PORT:-8000}}:8000"
    environment:
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "8000"
    read_only: true
    tmpfs:
      - /tmp:size=64m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
    mem_limit: ${{MCP_MEMORY_LIMIT:-1g}}
    cpus: ${{MCP_CPU_LIMIT:-1.0}}
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8000), 2).close()"]
      interval: 10s
      timeout: 3s
      retries: 6
      start_period: 20s
'''


def _dockerignore_source() -> str:
    return '''.git
.gitignore
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.venv/
venv/
dist/
build/
'''


def _copy_source(package: LoadedPackage, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if package.package_kind == "python-file":
        shutil.copy2(package.entry_file, destination / "main.py")
        return
    for source_file in iter_source_files(package):
        relative = source_file.relative_to(package.root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def generate_artifact(
    package: LoadedPackage,
    plan: PackagingPlan,
    output: Path,
    *,
    force: bool = False,
) -> Path:
    """Generate an artifact atomically and return its resolved directory."""
    output = output.expanduser().resolve()
    if output.exists():
        occupied = not output.is_dir() or any(output.iterdir())
        if occupied:
            if not force:
                raise FileExistsError(f"output path is not an empty directory: {output}")
            if output.is_dir():
                shutil.rmtree(output)
            else:
                output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        _copy_source(package, staging / "algorithm")
        requirements = [MCP_RUNTIME_REQUIREMENT, *plan.requirements]
        _write_text(staging / "requirements.txt", "\n".join(requirements) + "\n")
        _write_text(staging / "server.py", _server_source(plan))
        _write_text(staging / "_ioeb_verify.py", _verification_source())
        _write_text(staging / "Dockerfile", _dockerfile_source())
        _write_text(staging / "docker-compose.yml", _compose_source(plan))
        _write_text(staging / ".dockerignore", _dockerignore_source())
        artifact_manifest = {
            "artifactVersion": ARTIFACT_VERSION,
            **plan.to_dict(),
        }
        _write_text(
            staging / "ioeb-service.json",
            json.dumps(artifact_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        if output.exists():
            if output.is_dir():
                shutil.rmtree(output)
            else:
                output.unlink()
        os.replace(staging, output)
        return output
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
