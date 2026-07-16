"""Deterministic, non-semantic artifact scaffold for the builder Agent."""

from __future__ import annotations

import json
import re
import shutil
import keyword
from pathlib import Path
from typing import Any

from micro_agent.packaging.models import PackagingPlan


IGNORED_NAMES = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
    "env", "node_modules", "dist", "build", ".env",
}
IGNORED_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".pyc"}


def prepare_artifact(project_dir: str | Path, artifact_dir: str | Path, plan: PackagingPlan) -> Path:
    """Create safe deployment boilerplate and copy submitted code verbatim.

    Service semantics, adapters and MCP tool implementations are intentionally
    absent here; the builder Agent must create them from the validated plan.
    """
    source_root = Path(project_dir).resolve()
    output_root = Path(artifact_dir).resolve()
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)

    algorithm_root = output_root / "algorithm"
    algorithm_root.mkdir()
    _copy_project(source_root, algorithm_root)
    (algorithm_root / "__init__.py").touch(exist_ok=True)

    requirements = _read_requirements(source_root)
    requirements = _merge_requirements(
        requirements,
        ["mcp>=1.28.0,<2", "starlette>=0.37.0,<2", "uvicorn[standard]>=0.30.0,<1"],
    )
    (output_root / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")

    service_id = plan.data["services"][0]["id"]
    compose_name = re.sub(r"[^a-z0-9_-]", "-", service_id.lower()).strip("-") or "mcp-service"
    (output_root / "Dockerfile").write_text(
        "FROM python:3.11-slim\n"
        "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
        "WORKDIR /app\n"
        "COPY requirements.txt /app/requirements.txt\n"
        "RUN pip install --no-cache-dir --index-url \"${PIP_INDEX_URL}\" "
        "--timeout 120 --retries 5 -r /app/requirements.txt\n"
        "COPY . /app\n"
        "EXPOSE 8000\n"
        "CMD [\"python\", \"server.py\"]\n",
        encoding="utf-8",
    )
    (output_root / "docker-compose.yml").write_text(
        "services:\n"
        f"  {compose_name}:\n"
        "    build: .\n"
        "    restart: unless-stopped\n"
        "    ports:\n"
        "      - \"8000\"\n"
        "    environment:\n"
        "      - PYTHONUNBUFFERED=1\n",
        encoding="utf-8",
    )
    (output_root / "packaging_plan.json").write_text(plan.to_json() + "\n", encoding="utf-8")
    (output_root / "ioeb-service.json").write_text(
        json.dumps(
            {
                "schemaVersion": "ioeb.mcp-service/v1",
                "engine": "agentic",
                "transport": "sse",
                "endpoint": "/sse",
                "messagesEndpoint": "/messages/",
                "port": 8000,
                "services": [
                    {
                        "id": service["id"],
                        "name": service["name"],
                        "description": service["description"],
                        "tools": [tool["name"] for tool in service["tools"]],
                    }
                    for service in plan.data["services"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_root / "algorithm_loader.py").write_text(
        '"""Stable import boundary for the submitted algorithm repository."""\n'
        "\n"
        "from pathlib import Path\n"
        "import sys\n"
        "\n"
        "ALGORITHM_DIR = Path(__file__).resolve().parent / \"algorithm\"\n"
        "if str(ALGORITHM_DIR) not in sys.path:\n"
        "    sys.path.insert(0, str(ALGORITHM_DIR))\n",
        encoding="utf-8",
    )
    shutil.copy2(Path(__file__).with_name("runtime_guardrails.py"), output_root / "runtime_guardrails.py")
    (output_root / "server.py").write_text(_render_server(plan), encoding="utf-8")
    return output_root


def _copy_project(source_root: Path, algorithm_root: Path) -> None:
    for source in sorted(source_root.rglob("*")):
        rel = source.relative_to(source_root)
        if any(part in IGNORED_NAMES for part in rel.parts):
            continue
        if source.is_symlink() or not source.is_file():
            continue
        if source.suffix.lower() in IGNORED_SUFFIXES:
            continue
        target = algorithm_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _read_requirements(root: Path) -> list[str]:
    path = root / "requirements.txt"
    if not path.is_file():
        return []
    return [line.rstrip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]


def _merge_requirements(original: list[str], required: list[str]) -> list[str]:
    forced_names = {"mcp", "starlette", "uvicorn"}
    result = [
        line
        for line in original
        if line.strip() and _requirement_name(line) not in forced_names
    ]
    package_names = {
        re.split(r"[<>=!~\[\s]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")
        for line in result
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    }
    for requirement in required:
        name = re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].lower().replace("_", "-")
        if name not in package_names:
            result.append(requirement)
    return result


def _requirement_name(line: str) -> str:
    if not line.strip() or line.lstrip().startswith(("#", "-")):
        return ""
    return re.split(r"[<>=!~\[\s]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _render_server(plan: PackagingPlan) -> str:
    """Render protocol boilerplate from the Agent-reviewed capability contract.

    The renderer makes no service-design decisions. Keeping transport code out
    of the LLM edit surface prevents syntactically valid but unusable MCP
    variants such as ``from mcp import mcp``.
    """
    service_name = plan.data["services"][0]["name"]
    lines = [
        '"""Generated MCP protocol boundary; semantic adapters live in adapters.py."""',
        "",
        "from typing import Any",
        "",
        "import uvicorn",
        "from mcp.server.fastmcp import FastMCP",
        "",
        "import adapters",
        "",
        f"mcp = FastMCP({service_name!r}, host=\"0.0.0.0\", port=8000, sse_path=\"/sse\", message_path=\"/messages/\")",
        "",
    ]
    for tool in plan.tools:
        properties = tool["inputSchema"].get("properties", {})
        required = set(tool["inputSchema"].get("required", []))
        if not isinstance(properties, dict):
            properties = {}
        ordered_names = [name for name in properties if name in required] + [
            name for name in properties if name not in required
        ]
        params: list[str] = []
        for name in ordered_names:
            if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError(f"工具 {tool['name']} 包含非法 Python 参数名: {name}")
            annotation = _python_type(properties[name])
            default = "" if name in required else " = None"
            if name not in required:
                annotation = f"{annotation} | None"
            params.append(f"{name}: {annotation}{default}")
        return_type = _python_type(tool["outputSchema"])
        lines.extend(
            [
                "@mcp.tool()",
                f"def {tool['name']}({', '.join(params)}) -> {return_type}:",
                f"    {json.dumps(tool['description'], ensure_ascii=False)}",
                f"    return adapters.{tool['name']}({', '.join(f'{name}={name}' for name in ordered_names)})",
                "",
            ]
        )
    lines.extend(
        [
            "starlette_app = mcp.sse_app()",
            "",
            'if __name__ == "__main__":',
            '    uvicorn.run(starlette_app, host="0.0.0.0", port=8000)',
            "",
        ]
    )
    return "\n".join(lines)


def _python_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "Any"
    type_name = schema.get("type")
    if isinstance(type_name, list):
        non_null = [item for item in type_name if item != "null"]
        inner = _python_type({**schema, "type": non_null[0]}) if len(non_null) == 1 else "Any"
        return f"{inner} | None" if "null" in type_name else inner
    if type_name == "string":
        return "str"
    if type_name == "integer":
        return "int"
    if type_name == "number":
        return "float"
    if type_name == "boolean":
        return "bool"
    if type_name == "array":
        return f"list[{_python_type(schema.get('items', {}))}]"
    if type_name == "object":
        return "dict[str, Any]"
    return "Any"
