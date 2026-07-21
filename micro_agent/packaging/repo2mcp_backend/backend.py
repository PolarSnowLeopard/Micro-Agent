"""Isolated application bridge around the Repo2MCP paper implementation."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from micro_agent.core.config import LLMConfig


_IGNORED_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".env",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
}
_IGNORED_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".pyc"}


@dataclass(frozen=True)
class Repo2MCPBackendConfig:
    """Runtime knobs kept compatible with the v8 experiment defaults."""

    model: str
    api_key: str | None = None
    temperature: float = 0.0
    max_tokens: int = 8192
    reasoning_enabled: bool = False
    analysis_steps: int = 15
    generation_steps: int = 20
    fix_steps: int = 15
    max_fix_retries: int = 3
    verbose: bool = True

    @classmethod
    def from_llm_config(cls, config: LLMConfig) -> "Repo2MCPBackendConfig":
        return cls(
            model=config.model,
            api_key=config.api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_enabled=(
                config.reasoning_enabled
                if config.reasoning_enabled is not None
                else False
            ),
        )


@dataclass(frozen=True)
class Repo2MCPRun:
    """Prepared subprocess execution and its deterministic filesystem contract."""

    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    request_path: Path
    result_path: Path
    workspace_base: Path
    paper_output_dir: Path
    artifact_dir: Path
    sample_id: str
    project_dir: Path
    analysis_only: bool

    def load_result(self) -> dict[str, Any]:
        if not self.result_path.is_file():
            return {
                "success": False,
                "stage": "subprocess",
                "message": "Repo2MCP subprocess did not write a result",
            }
        return json.loads(self.result_path.read_text(encoding="utf-8"))


class Repo2MCPBackend:
    """Stage uploads and execute v8 without importing its global modules in-process."""

    def __init__(
        self,
        config: Repo2MCPBackendConfig,
        *,
        vendor_root: str | Path | None = None,
    ) -> None:
        self.config = config
        self.vendor_root = (
            Path(vendor_root).resolve()
            if vendor_root is not None
            else Path(__file__).with_name("vendor").resolve()
        )

    def prepare_run(
        self,
        *,
        project_dir: str | Path,
        job_root: str | Path,
        sample_id: str,
        wrap_intent: str,
        analysis_only: bool = False,
        tool_design: dict[str, Any] | None = None,
    ) -> Repo2MCPRun:
        project = Path(project_dir).resolve()
        root = Path(job_root).resolve()
        if not project.is_dir():
            raise ValueError(f"算法项目目录不存在: {project}")
        safe_id = _safe_sample_id(sample_id)
        if not wrap_intent.strip():
            wrap_intent = "分析仓库并封装其中稳定、面向用户的算法能力。"

        workspace_base = root / "repo2mcp-workspace"
        paper_output_dir = root / "repo2mcp-output"
        artifact_dir = root / "artifact"
        source_dir = workspace_base / safe_id / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        source_dir.mkdir(parents=True)
        _copy_project(project, source_dir)
        # v8 skips network cloning whenever this marker exists.  Uploaded
        # repositories need not contain or expose their original Git metadata.
        (source_dir / ".git").mkdir()
        paper_output_dir.mkdir(parents=True, exist_ok=True)

        request_path = root / "repo2mcp-request.json"
        result_path = root / "repo2mcp-result.json"
        request = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "reasoning_enabled": self.config.reasoning_enabled,
            "analysis_steps": self.config.analysis_steps,
            "generation_steps": self.config.generation_steps,
            "fix_steps": self.config.fix_steps,
            "max_fix_retries": self.config.max_fix_retries,
            "verbose": self.config.verbose,
            "output_dir": str(paper_output_dir),
            "workspace_base": str(workspace_base),
            "sample_id": safe_id,
            "wrap_intent": wrap_intent,
            "analysis_only": analysis_only,
        }
        if tool_design is not None:
            request["tool_design"] = tool_design
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        allowed_env = {
            "PATH",
            "HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "DOCKER_HOST",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed_env}
        env["PYTHONPATH"] = str(self.vendor_root)
        if self.config.api_key:
            env["REPO2MCP_API_KEY"] = self.config.api_key
        return Repo2MCPRun(
            command=(
                sys.executable,
                "-u",
                str(self.vendor_root / "run_request.py"),
                str(request_path),
                str(result_path),
            ),
            cwd=self.vendor_root,
            env=env,
            request_path=request_path,
            result_path=result_path,
            workspace_base=workspace_base,
            paper_output_dir=paper_output_dir,
            artifact_dir=artifact_dir,
            sample_id=safe_id,
            project_dir=project,
            analysis_only=analysis_only,
        )

    def finalize_artifact(
        self,
        run: Repo2MCPRun,
        result: dict[str, Any] | None = None,
    ) -> Path:
        """Turn benchmark-format output into the platform deployment contract."""
        if run.analysis_only:
            raise ValueError("analysis-only run has no deployable artifact")
        result = result or run.load_result()
        if not result.get("success"):
            raise RuntimeError(str(result.get("message") or "Repo2MCP generation failed"))
        source = run.paper_output_dir / run.sample_id
        if not source.is_dir():
            raise RuntimeError(f"Repo2MCP output directory is missing: {source}")

        if run.artifact_dir.exists():
            shutil.rmtree(run.artifact_dir)
        shutil.copytree(source, run.artifact_dir)
        repo_dir = run.artifact_dir / "repo"
        repo_dir.mkdir()
        _copy_project(run.project_dir, repo_dir)

        tool_design = _load_tool_design(run)
        graph = tool_design_to_frontend_graph(tool_design)
        (run.artifact_dir / "function.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run.artifact_dir / "tool_design.json").write_text(
            json.dumps(tool_design, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run.artifact_dir / "docker-compose.yml").write_text(
            "services:\n"
            "  mcp-service:\n"
            "    build: .\n"
            "    restart: unless-stopped\n"
            "    ports:\n"
            "      - \"8000\"\n"
            "    environment:\n"
            "      - PYTHONUNBUFFERED=1\n",
            encoding="utf-8",
        )
        tools = [
            str(item.get("name"))
            for item in tool_design.get("tools", [])
            if isinstance(item, dict) and item.get("name")
        ]
        metadata = {
            "schemaVersion": "ioeb.mcp-service/v1",
            "engine": "agentic",
            "generator": "repo2mcp-v8",
            "transport": "sse",
            "endpoint": "/sse",
            "messagesEndpoint": "/messages/",
            "port": 8000,
            "services": [
                {
                    "id": "repository_algorithm_service",
                    "name": "Repository Algorithm Service",
                    "description": "Agent-abstracted algorithm capabilities",
                    "tools": tools,
                }
            ],
        }
        (run.artifact_dir / "ioeb-service.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _harden_dockerfile(run.artifact_dir / "Dockerfile")
        marker = {
            "engine": "repo2mcp-v8",
            "toolCount": len(tools),
            "paperBuildVerified": True,
            "usage": result.get("usage", {}),
        }
        (run.artifact_dir / ".ioeb-ready").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return run.artifact_dir


def tool_design_to_frontend_graph(tool_design: dict[str, Any]) -> dict[str, Any]:
    """Convert the paper's semantic tool design into the unchanged UI schema."""
    raw_tools = tool_design.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ValueError("Repo2MCP tool_design.json 未包含任何工具")
    nodes: list[dict[str, Any]] = []
    for index, tool in enumerate(raw_tools):
        if not isinstance(tool, dict) or not str(tool.get("name", "")).strip():
            raise ValueError(f"Repo2MCP 工具设计第 {index + 1} 项无有效名称")
        inputs: list[str] = []
        for parameter in tool.get("parameters", []):
            if not isinstance(parameter, dict) or not parameter.get("name"):
                continue
            optional = "" if parameter.get("required") else "?"
            inputs.append(
                f"{parameter['name']}{optional}: {_json_type(parameter.get('type'))}"
            )
        returns = tool.get("returns") if isinstance(tool.get("returns"), dict) else {}
        implementation = (
            tool.get("implementation")
            if isinstance(tool.get("implementation"), dict)
            else {}
        )
        nodes.append(
            {
                "id": str(9001 + index),
                "x": (index % 4) * 180,
                "y": (index // 4) * 140,
                "label": str(tool["name"]),
                "size": 50,
                "input": ", ".join(inputs) or "None",
                "output": _json_type(returns.get("type")),
                "description": str(tool.get("description", "")),
                "environment": "Repo2MCP v8 Agent",
                "process": str(
                    implementation.get("notes")
                    or implementation.get("verified_import")
                    or "Agent-generated adapter"
                ),
                "apiType": "mcp",
                "methodType": "tool",
                "inputType": "json",
                "outputType": "json",
                "mcpType": "tool",
                "serviceId": "repository_algorithm_service",
                "sourceSymbols": [
                    item
                    for item in (
                        implementation.get("function_or_class"),
                        implementation.get("source_file"),
                    )
                    if item
                ],
            }
        )
    return {
        "nodes": nodes,
        "edges": [],
        "meta": {
            "schemaVersion": "ioeb.repo2mcp-tool-design/v8",
            "engine": "repo2mcp-v8",
            "serviceCount": 1,
            "toolCount": len(nodes),
            "analysisSummary": (
                f"Repo2MCP v8 Agent 从仓库中抽象出 {len(nodes)} 个 MCP 工具。"
            ),
        },
    }


def _load_tool_design(run: Repo2MCPRun) -> dict[str, Any]:
    path = run.workspace_base / run.sample_id / "tool_design.json"
    if not path.is_file():
        result = run.load_result()
        design = result.get("tool_design")
        if isinstance(design, dict):
            return design
        raise RuntimeError("Repo2MCP did not produce tool_design.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_sample_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    return safe[:96] or "uploaded-repository"


def _copy_project(source_root: Path, target_root: Path) -> None:
    for source in sorted(source_root.rglob("*")):
        relative = source.relative_to(source_root)
        if any(part in _IGNORED_NAMES for part in relative.parts):
            continue
        if source.is_symlink() or not source.is_file():
            continue
        if source.suffix.lower() in _IGNORED_SUFFIXES:
            continue
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _json_type(value: Any) -> str:
    normalized = str(value or "any").lower()
    return {
        "dict": "object",
        "mapping": "object",
        "list": "array",
        "tuple": "array",
        "boolean": "boolean",
        "bool": "boolean",
        "number": "number",
        "float": "number",
        "integer": "integer",
        "int": "integer",
        "str": "string",
    }.get(normalized, normalized)


def _harden_dockerfile(path: Path) -> None:
    if not path.is_file():
        return
    cpu_requirements = path.parent / "requirements-cpu.txt"
    cpu_requirements.touch(exist_ok=True)
    text = path.read_text(encoding="utf-8", errors="replace")
    cpu_copy = "COPY requirements.txt requirements-cpu.txt /app/"
    text = text.replace("COPY requirements.txt /app/requirements.txt", cpu_copy)
    if "PYTORCH_CPU_INDEX_URL" not in text:
        text = text.replace(
            cpu_copy,
            cpu_copy
            + "\nARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu"
            + "\nRUN if [ -s /app/requirements-cpu.txt ]; then "
            + "pip install --no-cache-dir --index-url "
            + "\"${PYTORCH_CPU_INDEX_URL}\" --timeout 120 --retries 5 "
            + "-r /app/requirements-cpu.txt; fi",
        )
    original = "RUN pip install --no-cache-dir -r /app/requirements.txt"
    replacement = (
        "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "RUN pip install --no-cache-dir --index-url \"${PIP_INDEX_URL}\" "
        "--timeout 120 --retries 5 -r /app/requirements.txt"
    )
    if original in text:
        text = text.replace(original, replacement)
    path.write_text(text, encoding="utf-8")
