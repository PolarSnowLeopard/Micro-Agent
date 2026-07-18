"""Deterministic, non-semantic artifact scaffold for the builder Agent."""

from __future__ import annotations

import ast
import configparser
import json
import keyword
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

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

    source_owned_distributions = _source_owned_distributions(source_root)
    declared_requirements = _merge_requirements(
        _read_requirements(source_root),
        _read_project_dependencies(source_root),
    )
    source_requirements = [
        requirement
        for requirement in declared_requirements
        if canonicalize_name(_requirement_name(requirement))
        not in source_owned_distributions
    ]
    cpu_requirements = [
        requirement
        for requirement in source_requirements
        if _requirement_name(requirement) in {"torch", "torchvision", "torchaudio"}
    ]
    requirements = _merge_requirements(
        [
            requirement
            for requirement in source_requirements
            if _requirement_name(requirement) not in {"torch", "torchvision", "torchaudio"}
        ],
        ["mcp>=1.28.0,<2", "starlette>=0.37.0,<2", "uvicorn[standard]>=0.30.0,<1"],
    )
    (output_root / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    (output_root / "requirements-cpu.txt").write_text(
        "\n".join(cpu_requirements) + ("\n" if cpu_requirements else ""),
        encoding="utf-8",
    )
    (output_root / "system-packages.txt").write_text("", encoding="utf-8")

    service_id = plan.data["services"][0]["id"]
    compose_name = re.sub(r"[^a-z0-9_-]", "-", service_id.lower()).strip("-") or "mcp-service"
    (output_root / "Dockerfile").write_text(
        "FROM python:3.11-slim-bookworm\n"
        "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "ARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1\n"
        "WORKDIR /app\n"
        "COPY system-packages.txt /app/system-packages.txt\n"
        "RUN set -eux; "
        "if [ -s /app/system-packages.txt ]; then "
        "apt-get update; "
        "sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' /app/system-packages.txt "
        "| xargs -r apt-get install -y --no-install-recommends; "
        "rm -rf /var/lib/apt/lists/*; "
        "fi\n"
        "COPY requirements.txt requirements-cpu.txt /app/\n"
        "RUN set -eux; "
        "if [ -s /app/requirements-cpu.txt ]; then "
        "pip install --no-cache-dir --index-url \"${PYTORCH_CPU_INDEX_URL}\" "
        "--timeout 120 --retries 5 -r /app/requirements-cpu.txt; "
        "fi\n"
        "RUN pip install --no-cache-dir --index-url \"${PIP_INDEX_URL}\" "
        "--timeout 120 --retries 5 -r /app/requirements.txt\n"
        "RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ioeb\n"
        "COPY --chown=10001:10001 . /app\n"
        "USER 10001:10001\n"
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
        "ALGORITHM_IMPORT_DIRS = tuple(\n"
        "    path for path in (ALGORITHM_DIR, ALGORITHM_DIR / \"src\") if path.is_dir()\n"
        ")\n"
        "# Append after site-packages so an incomplete source checkout cannot shadow\n"
        "# an installed runtime dependency with the same package name (for example rdkit).\n"
        "for path in ALGORITHM_IMPORT_DIRS:\n"
        "    if str(path) not in sys.path:\n"
        "        sys.path.append(str(path))\n",
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
    requirements: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        if requirement.url:
            continue
        requirements.append(line)
    return requirements


def _read_project_dependencies(root: Path) -> list[str]:
    """Read install dependencies from packaging metadata without executing it."""

    candidates: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            dependencies = tomllib.loads(
                pyproject.read_text(encoding="utf-8", errors="replace")
            ).get("project", {}).get("dependencies", [])
            if isinstance(dependencies, list):
                candidates.extend(item for item in dependencies if isinstance(item, str))
        except (OSError, tomllib.TOMLDecodeError):
            pass

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(setup_cfg, encoding="utf-8")
            raw = parser.get("options", "install_requires", fallback="")
            candidates.extend(raw.splitlines())
        except (OSError, configparser.Error):
            pass

    setup_py = root / "setup.py"
    if setup_py.is_file():
        try:
            tree = ast.parse(setup_py.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                dependency_node: ast.AST | None = None
                if isinstance(node, ast.keyword) and node.arg == "install_requires":
                    dependency_node = node.value
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "install_requires"
                        ):
                            dependency_node = value
                            break
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                    if any(
                        isinstance(target, ast.Name)
                        and target.id == "install_requires"
                        for target in targets
                    ):
                        dependency_node = node.value
                if dependency_node is not None:
                    candidates.extend(_static_requirement_values(dependency_node))

    valid: list[str] = []
    for raw in candidates:
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        if requirement.url:
            continue
        valid.append(line)
    return _merge_requirements([], valid)


def _static_requirement_values(node: ast.AST) -> list[str]:
    """Extract literal requirement strings, selecting the Python 3.11 branch."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: list[str] = []
        for element in node.elts:
            values.extend(_static_requirement_values(element))
        return values
    if isinstance(node, ast.IfExp):
        condition = _static_python_version_condition(node.test)
        if condition is True:
            return _static_requirement_values(node.body)
        if condition is False:
            return _static_requirement_values(node.orelse)
        return (
            _static_requirement_values(node.body)
            + _static_requirement_values(node.orelse)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (
            _static_requirement_values(node.left)
            + _static_requirement_values(node.right)
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"list", "tuple", "set"}
        and len(node.args) == 1
    ):
        return _static_requirement_values(node.args[0])
    return []


def _static_python_version_condition(node: ast.AST) -> bool | None:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _static_python_version_condition(node.operand)
        return None if value is None else not value
    if isinstance(node, ast.BoolOp):
        values = [_static_python_version_condition(value) for value in node.values]
        if any(value is None for value in values):
            return None
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        return None
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
        return None
    left = _static_python_version_value(node.left)
    right = _static_python_version_value(node.comparators[0])
    if left is None or right is None:
        return None
    operation = node.ops[0]
    try:
        if isinstance(operation, ast.Eq):
            return left == right
        if isinstance(operation, ast.NotEq):
            return left != right
        if isinstance(operation, ast.Gt):
            return left > right
        if isinstance(operation, ast.GtE):
            return left >= right
        if isinstance(operation, ast.Lt):
            return left < right
        if isinstance(operation, ast.LtE):
            return left <= right
    except TypeError:
        return None
    return None


def _static_python_version_value(node: ast.AST) -> int | tuple[int, ...] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if (
        isinstance(node, (ast.Tuple, ast.List))
        and all(
            isinstance(element, ast.Constant) and isinstance(element.value, int)
            for element in node.elts
        )
    ):
        return tuple(element.value for element in node.elts)
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "sys"
        and node.value.attr == "version_info"
    ):
        return {"major": 3, "minor": 11, "micro": 0}.get(node.attr)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "sys"
        and node.value.attr == "version_info"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        return (3, 11, 0)[node.slice.value] if 0 <= node.slice.value < 3 else None
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "version_info"
    ):
        return (3, 11, 0)
    return None


def _source_owned_distributions(root: Path) -> set[str]:
    """Find pure-Python distributions that should run from the submitted source.

    Installing a second PyPI copy of the repository can silently replace the
    reviewed code with a different release. Compiled projects remain declared
    because their checked-out Python wrappers may require wheel-provided native
    extensions.
    """
    if _uses_compiled_build(root):
        return set()
    owned: set[str] = set()
    for name in _declared_project_names(root):
        import_name = name.replace("-", "_").replace(".", "_")
        if any(
            (root / prefix / import_name / "__init__.py").is_file()
            for prefix in (Path(), Path("src"), Path("python"), Path("lib"))
        ):
            owned.add(canonicalize_name(name))
    return owned


def _declared_project_names(root: Path) -> set[str]:
    names: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            value = tomllib.loads(
                pyproject.read_text(encoding="utf-8", errors="replace")
            ).get("project", {}).get("name")
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
        except (OSError, tomllib.TOMLDecodeError):
            pass

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(setup_cfg, encoding="utf-8")
            value = parser.get("metadata", "name", fallback="").strip()
            if value:
                names.add(value)
        except (OSError, configparser.Error):
            pass

    setup_py = root / "setup.py"
    if setup_py.is_file():
        try:
            tree = ast.parse(setup_py.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            tree = None
        if tree is not None:
            unpacked_setup_kwargs: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not (
                    (isinstance(node.func, ast.Name) and node.func.id == "setup")
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "setup"
                    )
                ):
                    continue
                unpacked_setup_kwargs.update(
                    keyword_arg.value.id
                    for keyword_arg in node.keywords
                    if keyword_arg.arg is None
                    and isinstance(keyword_arg.value, ast.Name)
                )
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name)
                    and target.id in unpacked_setup_kwargs
                    for target in node.targets
                ):
                    if (
                        isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "dict"
                    ):
                        for keyword_arg in node.value.keywords:
                            if (
                                keyword_arg.arg == "name"
                                and isinstance(keyword_arg.value, ast.Constant)
                                and isinstance(keyword_arg.value.value, str)
                            ):
                                names.add(keyword_arg.value.value.strip())
                    elif isinstance(node.value, ast.Dict):
                        for key, value in zip(node.value.keys, node.value.values):
                            if (
                                isinstance(key, ast.Constant)
                                and key.value == "name"
                                and isinstance(value, ast.Constant)
                                and isinstance(value.value, str)
                            ):
                                names.add(value.value.strip())
                if not isinstance(node, ast.Call):
                    continue
                if not (
                    (isinstance(node.func, ast.Name) and node.func.id == "setup")
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "setup"
                    )
                ):
                    continue
                for keyword_arg in node.keywords:
                    if (
                        keyword_arg.arg == "name"
                        and isinstance(keyword_arg.value, ast.Constant)
                        and isinstance(keyword_arg.value.value, str)
                    ):
                        names.add(keyword_arg.value.value.strip())
    return {name for name in names if name}


def _uses_compiled_build(root: Path) -> bool:
    markers = (
        "ext_modules",
        "Extension(",
        "cythonize(",
        "RustExtension",
        "CMakeExtension",
        "maturin",
        "mesonpy",
        "scikit_build",
        "setuptools_rust",
    )
    for name in ("setup.py", "setup.cfg", "pyproject.toml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(marker.lower() in text.lower() for marker in markers):
            return True
    return False


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
            package_names.add(name)
    return result


def _requirement_name(line: str) -> str:
    if not line.strip() or line.lstrip().startswith(("#", "-")):
        return ""
    try:
        return Requirement(line).name.lower().replace("_", "-")
    except InvalidRequirement:
        return ""


def _render_server(plan: PackagingPlan) -> str:
    """Render protocol boilerplate from the Agent-reviewed capability contract.

    The renderer makes no service-design decisions. Keeping transport code out
    of the LLM edit surface prevents syntactically valid but unusable MCP
    variants such as ``from mcp import mcp``.
    """
    service_name = plan.data["services"][0]["name"]
    type_renderer = _SchemaTypeRenderer()
    rendered_tools = []
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
            schema = properties[name] if isinstance(properties[name], dict) else {}
            annotation = type_renderer.annotation(
                schema,
                suggested_name=f"{tool['name']}_{name}_input",
            )
            annotation = _annotated_field(annotation, schema)
            if name in required:
                default = ""
            else:
                default_value = schema.get("default")
                default = f" = {default_value!r}"
            params.append(f"{name}: {annotation}{default}")
        return_type = type_renderer.annotation(
            tool["outputSchema"],
            suggested_name=f"{tool['name']}_output",
            model_objects=True,
        )
        rendered_tools.append(
            {
                "tool": tool,
                "ordered_names": ordered_names,
                "params": params,
                "return_type": return_type,
            }
        )

    lines = [
        '"""Generated MCP protocol boundary; semantic adapters live in adapters.py."""',
        "",
        "from typing import Annotated, Any, Literal",
        "from typing_extensions import NotRequired, Required, TypedDict",
        "",
        "from pydantic import BaseModel, Field, create_model",
        "",
        "import uvicorn",
        "from mcp.server.fastmcp import FastMCP",
        "",
        "import adapters",
        "",
        f"mcp = FastMCP({service_name!r}, host=\"0.0.0.0\", port=8000, sse_path=\"/sse\", message_path=\"/messages/\")",
        "",
        "def _bind_protocol_schema(",
        "    tool_name: str,",
        "    input_schema: dict[str, Any],",
        "    output_schema: dict[str, Any],",
        ") -> None:",
        '    """Publish the reviewed JSON Schemas without Pydantic default/null drift."""',
        "    registered = mcp._tool_manager.get_tool(tool_name)",
        "    if registered is None:",
        '        raise RuntimeError(f"MCP tool registration missing: {tool_name}")',
        "    registered.parameters = input_schema",
        "    registered.fn_metadata.output_schema = output_schema",
        '    registered.__dict__.pop("output_schema", None)',
        "",
        "class _IOEBOutputModel(BaseModel):",
        '    """Preserve optional-but-non-null JSON Schema fields on serialization."""',
        "    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:",
        '        kwargs.setdefault("exclude_unset", True)',
        "        return super().model_dump(*args, **kwargs)",
        "",
    ]
    if type_renderer.definitions:
        lines.extend(type_renderer.definitions)
        lines.append("")
    for rendered in rendered_tools:
        tool = rendered["tool"]
        ordered_names = rendered["ordered_names"]
        lines.extend(
            [
                "@mcp.tool()",
                f"def {tool['name']}({', '.join(rendered['params'])}) -> {rendered['return_type']}:",
                f"    {_render_tool_docstring(tool)!r}",
                f"    return adapters.{tool['name']}({', '.join(f'{name}={name}' for name in ordered_names)})",
                "",
                f"_bind_protocol_schema({tool['name']!r}, "
                f"{tool['inputSchema']!r}, {tool['outputSchema']!r})",
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


@dataclass
class _SchemaTypeRenderer:
    """Render JSON Schema as Python annotations FastMCP can publish unchanged."""

    definitions: list[str] = field(default_factory=list)
    _defined_names: set[str] = field(default_factory=set)

    def annotation(
        self,
        schema: Any,
        *,
        suggested_name: str,
        model_objects: bool = False,
    ) -> str:
        if not isinstance(schema, dict):
            return "Any"
        variants = schema.get("oneOf") or schema.get("anyOf")
        if isinstance(variants, list) and variants:
            rendered = [
                self.annotation(
                    item,
                    suggested_name=f"{suggested_name}_{index + 1}",
                    model_objects=model_objects,
                )
                for index, item in enumerate(variants)
            ]
            return " | ".join(dict.fromkeys(rendered))
        enum = schema.get("enum")
        if isinstance(enum, list) and enum and all(_literal_compatible(item) for item in enum):
            return f"Literal[{', '.join(repr(item) for item in enum)}]"
        if "const" in schema and _literal_compatible(schema["const"]):
            return f"Literal[{schema['const']!r}]"

        type_name = schema.get("type")
        if isinstance(type_name, list):
            rendered = [
                "None" if item == "null" else self.annotation(
                    {**schema, "type": item},
                    suggested_name=suggested_name,
                    model_objects=model_objects,
                )
                for item in type_name
            ]
            return " | ".join(dict.fromkeys(rendered))
        if type_name == "string":
            return "str"
        if type_name == "integer":
            return "int"
        if type_name == "number":
            return "float"
        if type_name == "boolean":
            return "bool"
        if type_name == "array":
            return (
                "list["
                + self.annotation(
                    schema.get("items", {}),
                    suggested_name=f"{suggested_name}_item",
                    model_objects=model_objects,
                )
                + "]"
            )
        if type_name == "object":
            properties = schema.get("properties")
            if isinstance(properties, dict) and properties:
                if model_objects:
                    return self._pydantic_model(schema, suggested_name)
                return self._typed_dict(schema, suggested_name)
            additional = schema.get("additionalProperties")
            value_type = (
                self.annotation(
                    additional,
                    suggested_name=f"{suggested_name}_value",
                    model_objects=model_objects,
                )
                if isinstance(additional, dict)
                else "Any"
            )
            return f"dict[str, {value_type}]"
        if type_name == "null":
            return "None"
        return "Any"

    def _typed_dict(self, schema: dict[str, Any], suggested_name: str) -> str:
        name = _python_type_name(suggested_name)
        candidate = name
        suffix = 2
        while candidate in self._defined_names:
            candidate = f"{name}{suffix}"
            suffix += 1
        name = candidate
        self._defined_names.add(name)

        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        entries: list[str] = []
        for property_name, raw_child in properties.items():
            child = raw_child if isinstance(raw_child, dict) else {}
            annotation = self.annotation(
                child,
                suggested_name=f"{name}_{property_name}",
            )
            wrapper = "Required" if property_name in required else "NotRequired"
            wrapped = f"{wrapper}[{annotation}]"
            wrapped = _annotated_field(wrapped, child)
            entries.append(f"{property_name!r}: {wrapped}")
        self.definitions.append(
            f"{name} = TypedDict({name!r}, {{{', '.join(entries)}}})"
        )
        return name

    def _pydantic_model(self, schema: dict[str, Any], suggested_name: str) -> str:
        name = self._unique_name(suggested_name)
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        entries: list[str] = []
        for property_name, raw_child in properties.items():
            child = raw_child if isinstance(raw_child, dict) else {}
            annotation = self.annotation(
                child,
                suggested_name=f"{name}_{property_name}",
                model_objects=True,
            )
            annotation = _annotated_field(annotation, child)
            if property_name in required:
                default = "..."
            else:
                default = repr(child.get("default"))
            entries.append(f"{property_name!r}: ({annotation}, {default})")
        self.definitions.append(
            f"{name} = create_model({name!r}, __base__=_IOEBOutputModel, "
            f"**{{{', '.join(entries)}}})"
        )
        return name

    def _unique_name(self, suggested_name: str) -> str:
        name = _python_type_name(suggested_name)
        candidate = name
        suffix = 2
        while candidate in self._defined_names:
            candidate = f"{name}{suffix}"
            suffix += 1
        self._defined_names.add(candidate)
        return candidate


def _annotated_field(annotation: str, schema: dict[str, Any]) -> str:
    arguments: list[str] = []
    description = schema.get("description")
    if isinstance(description, str) and description.strip():
        arguments.append(f"description={description.strip()!r}")
    title = schema.get("title")
    if isinstance(title, str) and title.strip():
        arguments.append(f"title={title.strip()!r}")

    mappings = {
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
    for schema_name, field_name in mappings.items():
        value = schema.get(schema_name)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            arguments.append(f"{field_name}={value!r}")
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        arguments.append(f"examples={examples!r}")

    extras = {
        key: schema[key]
        for key in ("format", "contentEncoding", "contentMediaType")
        if key in schema
    }
    if extras:
        arguments.append(f"json_schema_extra={extras!r}")
    if not arguments:
        return annotation
    return f"Annotated[{annotation}, Field({', '.join(arguments)})]"


def _render_tool_docstring(tool: dict[str, Any]) -> str:
    description = str(tool.get("description", "")).strip()
    lines = [description]
    properties = tool.get("inputSchema", {}).get("properties", {})
    if isinstance(properties, dict) and properties:
        lines.extend(["", "Args:"])
        required = set(tool.get("inputSchema", {}).get("required", []))
        for name, raw_schema in properties.items():
            schema = raw_schema if isinstance(raw_schema, dict) else {}
            detail = str(schema.get("description") or f"Input value for {name}.").strip()
            constraints = _describe_constraints(schema, required=name in required)
            if constraints:
                detail = f"{detail} {constraints}"
            lines.append(f"    {name}: {detail}")

    lines.extend(["", "Returns:", f"    {_describe_output(tool.get('outputSchema'))}"])
    lines.extend(
        [
            "",
            "Raises:",
            "    ValueError: If an input is invalid or the underlying algorithm cannot produce a valid result.",
        ]
    )
    return "\n".join(lines)


def _describe_constraints(schema: dict[str, Any], *, required: bool) -> str:
    details = ["Required." if required else "Optional."]
    if "default" in schema:
        details.append(f"Default: {schema['default']!r}.")
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        details.append(f"Allowed values: {', '.join(repr(item) for item in enum)}.")
    if "minimum" in schema:
        details.append(f"Minimum: {schema['minimum']!r}.")
    if "maximum" in schema:
        details.append(f"Maximum: {schema['maximum']!r}.")
    if "exclusiveMinimum" in schema:
        details.append(f"Must be greater than {schema['exclusiveMinimum']!r}.")
    if "exclusiveMaximum" in schema:
        details.append(f"Must be less than {schema['exclusiveMaximum']!r}.")
    if "pattern" in schema:
        details.append(f"Must match pattern {schema['pattern']!r}.")
    if "format" in schema:
        details.append(f"Format: {schema['format']}.")
    if "contentEncoding" in schema:
        details.append(f"Content encoding: {schema['contentEncoding']}.")
    return " ".join(details)


def _describe_output(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "A JSON-compatible result produced by the underlying algorithm."
    description = schema.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    type_name = schema.get("type")
    properties = schema.get("properties")
    if type_name == "object" and isinstance(properties, dict) and properties:
        fields = []
        for name, raw_child in properties.items():
            child = raw_child if isinstance(raw_child, dict) else {}
            child_type = child.get("type", "value")
            child_description = str(child.get("description", "")).strip()
            rendered = f"{name} ({child_type})"
            if child_description:
                rendered += f": {child_description}"
            fields.append(rendered)
        return "A structured JSON object with fields: " + "; ".join(fields) + "."
    if type_name == "array":
        return "A structured JSON array containing the algorithm results."
    return f"A JSON-compatible {type_name or 'result'} produced by the underlying algorithm."


def _schema_allows_null(schema: dict[str, Any]) -> bool:
    type_name = schema.get("type")
    if isinstance(type_name, list) and "null" in type_name:
        return True
    variants = schema.get("oneOf") or schema.get("anyOf")
    return bool(
        isinstance(variants, list)
        and any(isinstance(item, dict) and item.get("type") == "null" for item in variants)
    )


def _literal_compatible(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, bool))


def _python_type_name(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    rendered = "".join(part[:1].upper() + part[1:] for part in parts) or "GeneratedSchema"
    if rendered[0].isdigit():
        rendered = "Schema" + rendered
    return rendered


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
