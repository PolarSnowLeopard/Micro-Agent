"""Contract tests for the isolated Repo2MCP v8 backend bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from api.routes.agent import _mcp_packaging_engine
from micro_agent.core.config import LLMConfig
from micro_agent.packaging.repo2mcp_backend import (
    Repo2MCPBackend,
    Repo2MCPBackendConfig,
    tool_design_to_frontend_graph,
)


def _tool_design() -> dict:
    return {
        "tools": [
            {
                "name": "predict_risk",
                "description": "Predict risk from a validated customer record.",
                "parameters": [
                    {
                        "name": "customer",
                        "type": "dict",
                        "required": True,
                        "description": "Customer fields",
                    },
                    {
                        "name": "threshold",
                        "type": "float",
                        "required": False,
                        "default": 0.5,
                    },
                ],
                "returns": {"type": "dict", "description": "Risk result"},
                "implementation": {
                    "source_file": "model.py",
                    "function_or_class": "predict",
                    "verified_import": "from model import predict",
                    "notes": "Validate and normalize the record before inference.",
                },
            },
            {
                "name": "explain_risk",
                "description": "Explain an existing model risk prediction.",
                "parameters": [
                    {"name": "features", "type": "list", "required": True}
                ],
                "returns": {"type": "list"},
                "implementation": {
                    "source_file": "explain.py",
                    "function_or_class": "explain",
                },
            },
        ],
        "dependencies": ["numpy"],
    }


def test_tool_design_to_frontend_graph_preserves_all_agent_tools():
    graph = tool_design_to_frontend_graph(_tool_design())

    assert graph["meta"] == {
        "schemaVersion": "ioeb.repo2mcp-tool-design/v8",
        "engine": "repo2mcp-v8",
        "serviceCount": 1,
        "toolCount": 2,
        "analysisSummary": "Repo2MCP v8 Agent 从仓库中抽象出 2 个 MCP 工具。",
    }
    assert [node["label"] for node in graph["nodes"]] == [
        "predict_risk",
        "explain_risk",
    ]
    assert graph["nodes"][0]["input"] == "customer: object, threshold?: number"
    assert graph["nodes"][0]["output"] == "object"
    assert graph["nodes"][1]["output"] == "array"


def test_prepare_run_stages_upload_without_git_or_secrets(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def predict(value): return value\n", encoding="utf-8")
    (project / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("private\n", encoding="utf-8")

    backend = Repo2MCPBackend(
        Repo2MCPBackendConfig.from_llm_config(
            LLMConfig(
                model="openrouter/qwen/qwen3.6-flash",
                api_key="test-secret-not-persisted",
                max_tokens=8192,
                reasoning_enabled=False,
            )
        )
    )
    run = backend.prepare_run(
        project_dir=project,
        job_root=tmp_path / "job",
        sample_id="../../unsafe sample",
        wrap_intent="封装预测和解释能力",
        analysis_only=True,
    )

    request = json.loads(run.request_path.read_text(encoding="utf-8"))
    staged = run.workspace_base / run.sample_id / "source"
    assert run.sample_id == "unsafe-sample"
    assert (staged / "main.py").is_file()
    assert (staged / ".git").is_dir()
    assert not (staged / ".git" / "config").exists()
    assert not (staged / ".env").exists()
    assert request["model"] == "openrouter/qwen/qwen3.6-flash"
    assert request["reasoning_enabled"] is False
    assert "api_key" not in request
    assert run.env["REPO2MCP_API_KEY"] == "test-secret-not-persisted"
    assert "LLM_API_KEY" not in run.env
    assert "OPENROUTER_API_KEY" not in run.env


def test_finalize_artifact_adds_platform_contract_and_repo(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "algorithm.py").write_text("def predict(value): return value\n", encoding="utf-8")
    backend = Repo2MCPBackend(
        Repo2MCPBackendConfig(model="openrouter/qwen/qwen3.6-flash")
    )
    run = backend.prepare_run(
        project_dir=project,
        job_root=tmp_path / "job",
        sample_id="example",
        wrap_intent="封装预测能力",
        tool_design=_tool_design(),
    )
    request = json.loads(run.request_path.read_text(encoding="utf-8"))
    assert request["tool_design"] == _tool_design()
    paper_output = run.paper_output_dir / run.sample_id
    paper_output.mkdir(parents=True)
    (paper_output / "server.py").write_text("# generated\n", encoding="utf-8")
    (paper_output / "Dockerfile").write_text(
        "FROM python:3.11-slim\n"
        "COPY requirements.txt /app/requirements.txt\n"
        "RUN pip install --no-cache-dir -r /app/requirements.txt\n"
        "COPY repo/ /app/repo/\n",
        encoding="utf-8",
    )
    (paper_output / "requirements.txt").write_text("mcp\n", encoding="utf-8")
    design_path = run.workspace_base / run.sample_id / "tool_design.json"
    design_path.write_text(json.dumps(_tool_design()), encoding="utf-8")

    artifact = backend.finalize_artifact(
        run,
        {"success": True, "usage": {"calls": 4, "total_tokens": 100}},
    )

    assert (artifact / "repo" / "algorithm.py").is_file()
    assert (artifact / "docker-compose.yml").is_file()
    assert (artifact / "function.json").is_file()
    assert (artifact / "tool_design.json").is_file()
    assert (artifact / "requirements-cpu.txt").is_file()
    assert (artifact / "ioeb-service.json").is_file()
    metadata = json.loads((artifact / "ioeb-service.json").read_text(encoding="utf-8"))
    assert metadata["engine"] == "agentic"
    assert metadata["generator"] == "repo2mcp-v8"
    marker = json.loads((artifact / ".ioeb-ready").read_text(encoding="utf-8"))
    assert marker["engine"] == "repo2mcp-v8"
    assert marker["toolCount"] == 2
    dockerfile = (artifact / "Dockerfile").read_text(encoding="utf-8")
    assert "pypi.tuna.tsinghua.edu.cn" in dockerfile
    assert "download.pytorch.org/whl/cpu" in dockerfile
    assert "--timeout 120 --retries 5" in dockerfile


def test_packaging_engine_is_explicitly_opt_in(monkeypatch):
    monkeypatch.delenv("IOEB_MCP_PACKAGING_ENGINE", raising=False)
    assert _mcp_packaging_engine() == "agentic"
    monkeypatch.setenv("IOEB_MCP_PACKAGING_ENGINE", "repo2mcp")
    assert _mcp_packaging_engine() == "repo2mcp-v8"
    monkeypatch.setenv("IOEB_MCP_PACKAGING_ENGINE", "unknown")
    with pytest.raises(HTTPException):
        _mcp_packaging_engine()


def test_vendor_batch_config_honors_disabled_reasoning(tmp_path):
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import LLMConfig; "
                "print(LLMConfig(model='openrouter/qwen/qwen3.6-flash').reasoning_enabled)"
            ),
        ],
        cwd=vendor,
        env={"PATH": str(Path(sys.executable).parent), "LLM_REASONING_ENABLED": "false"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.strip() == "False"


def test_vendor_normalizes_import_names_before_docker_build(tmp_path):
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    output = tmp_path / "output"
    output.mkdir()
    requirements = output / "requirements.txt"
    requirements.write_text(
        (
            "mcp[cli]\nPIL>=8\ncv2\nsklearn\nopenslide\nPillow>=8\n"
            "torch>=2\ntorchvision\n"
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from src.wrapper import MCPWrapper; "
                "print(json.dumps(MCPWrapper._normalize_generated_requirements(sys.argv[1])))"
            ),
            str(output),
        ],
        cwd=vendor,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    fixes = json.loads(completed.stdout)
    assert len(fixes) == 6
    assert requirements.read_text(encoding="utf-8").splitlines() == [
        "mcp[cli]",
        "Pillow>=8",
        "opencv-python-headless",
        "scikit-learn",
        "openslide-python",
    ]
    assert (output / "requirements-cpu.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["torch>=2", "torchvision"]


def test_vendor_import_precheck_defers_external_packages(tmp_path):
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    source = tmp_path / "source"
    (source / "local_pkg").mkdir(parents=True)
    (source / "local_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (source / "local_pkg" / "valid.py").write_text("def run(): pass\n", encoding="utf-8")
    server = tmp_path / "server.py"
    server.write_text(
        "from local_pkg.valid import run\n"
        "from local_pkg.missing import absent\n"
        "from numpy.linalg import norm\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from src.wrapper import MCPWrapper; "
                "print(json.dumps(MCPWrapper._verify_imports(sys.argv[1], sys.argv[2], None)))"
            ),
            str(server),
            str(source),
        ],
        cwd=vendor,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout) == [
        "from local_pkg.missing import absent → FAIL"
    ]


def test_vendor_compacts_initial_darp_context_after_first_turn():
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.agent.mcp_agent import MCPAgent; "
                "from src.tools.base import ToolRegistry; "
                "a=MCPAgent(llm=None,tools=ToolRegistry(),compact_initial_task_after=1); "
                "a.messages.append({'role':'user','content':'HEAD'+('x'*20000)+'TAIL'}); "
                "a._compact_initial_task(1); "
                "print(len(a.messages[1]['content'])); "
                "print('DARP/BAGE' in a.messages[1]['content']); "
                "print(a.messages[1]['content'].endswith('TAIL'))"
            ),
        ],
        cwd=vendor,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    lines = completed.stdout.splitlines()
    assert int(lines[0]) < 9_000
    assert lines[1:] == ["True", "True"]


def test_vendor_stops_analysis_at_evidence_budget():
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.agent.mcp_agent import MCPAgent; "
                "from src.llm.client import LLMResponse,ToolCall; "
                "from src.tools.base import ToolRegistry; "
                "Fake=type('Fake',(),{'calls':0,'chat':lambda s,**k: "
                "(setattr(s,'calls',s.calls+1) or LLMResponse(None,[ToolCall('1','missing',{})],'tool_calls'))}); "
                "llm=Fake(); a=MCPAgent(llm=llm,tools=ToolRegistry(),max_steps=10,verbose=False,"
                "completion_check=lambda:False,force_completion_after=1); "
                "print(repr(a.run('task'))); print(llm.calls)"
            ),
        ],
        cwd=vendor,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.splitlines() == ["''", "1"]


def test_vendor_extracts_nested_tool_design_from_text(tmp_path):
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    target = tmp_path / "tool_design.json"
    response = (
        "prefix that is not JSON\n"
        '{"tools":[{"name":"balance_reaction","parameters":[],"implementation":'
        '{"source_file":"chempy/chemistry.py","notes":{"nested":true}}}],'
        '"dependencies":{"python":["chempy"]}}\ntrailing text'
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from src.wrapper import MCPWrapper; "
                "print(MCPWrapper._try_extract_json_from_response(sys.argv[1], sys.argv[2]))"
            ),
            response,
            str(target),
        ],
        cwd=vendor,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.splitlines()[-1] == "True"
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["tools"][0]["implementation"]["notes"]["nested"] is True


def test_vendor_bounds_structured_compiler_evidence():
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.wrapper import MCPWrapper; "
                "value=MCPWrapper._compact_analysis_evidence('HEAD'+('x'*30000)+'TAIL'); "
                "print(len(value)); print(value.startswith('HEAD')); print(value.endswith('TAIL'))"
            ),
        ],
        cwd=vendor,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    length, starts, ends = completed.stdout.splitlines()
    assert int(length) < 17_000
    assert (starts, ends) == ("True", "True")


def test_vendor_adds_only_reachable_declared_runtime_dependencies(tmp_path):
    vendor = (
        Path(__file__).parents[1]
        / "micro_agent"
        / "packaging"
        / "repo2mcp_backend"
        / "vendor"
    )
    source = tmp_path / "source"
    output = tmp_path / "output"
    (source / "models").mkdir(parents=True)
    output.mkdir()
    (source / "main.py").write_text(
        "from models.eomt import run\n",
        encoding="utf-8",
    )
    (source / "models" / "eomt.py").write_text(
        "import torch\nimport timm\nimport transformers\n"
        "from PIL import Image\ndef run(): pass\n",
        encoding="utf-8",
    )
    (source / "requirements.txt").write_text(
        "timm==1.0.15\ntorch==2.7.0\nPillow==11.1.0\npytest\n",
        encoding="utf-8",
    )
    server = output / "server.py"
    server.write_text("from main import run\n", encoding="utf-8")
    requirements = output / "requirements.txt"
    requirements.write_text("mcp[cli]\nPillow\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from src.wrapper import MCPWrapper; "
                "print(json.dumps(MCPWrapper._merge_declared_runtime_dependencies("
                "sys.argv[1],sys.argv[2],sys.argv[3])))"
            ),
            str(server),
            str(source),
            str(output),
        ],
        cwd=vendor,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    fixes = json.loads(completed.stdout)
    assert len(fixes) == 3
    assert requirements.read_text(encoding="utf-8").splitlines() == [
        "mcp[cli]",
        "Pillow",
        "timm==1.0.15",
        "torch==2.7.0",
        "transformers",
    ]
