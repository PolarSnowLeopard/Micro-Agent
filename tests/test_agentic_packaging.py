"""Contract tests for the Agent-based MCP packaging pipeline."""

from __future__ import annotations

import ast
import importlib.util
import json
import base64
import asyncio
import io
import subprocess
import sys
import zipfile
from pathlib import Path

from micro_agent.core.schema import AgentEvent
from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.capability_coverage import assess_dispatch_coverage
from micro_agent.packaging.dependency_inspector import unresolved_import_dependencies
from micro_agent.packaging.interface_quality import assess_interface_quality
from micro_agent.packaging.models import PackagingPlan, PlanValidationError
from micro_agent.packaging.relevance import build_relevance_evidence
from micro_agent.packaging.scaffold import prepare_artifact
from micro_agent.packaging.runtime_guardrails import decode_safe_zip
from micro_agent.packaging.runtime_verifier import (
    ContainerRuntimeVerifier,
    PROBE_MARKER,
    _runtime_probe_source,
)
from micro_agent.packaging.tools import (
    PatchArtifactFile,
    PlanStore,
    ReadProjectFile,
    ReviseSmokeTests,
    SavePackagingPlan,
    SavePackagingPlanJson,
    WriteArtifactFile,
    _canonical_smoke_input,
    _file_path_suggestions,
    _smoke_errors_prove_fixture_grounding,
    _smoke_string_candidates,
)
from micro_agent.packaging.verifier import ArtifactVerifier, VerificationReport
from micro_agent.packaging.workflow import (
    AgenticAnalysisWorkflow,
    AgenticPackagingWorkflow,
    AnalysisCache,
    _build_builder_agent,
    _build_planning_agent,
    _builder_implementation_context,
    _builder_prompt,
    _configure_repair_builder,
    _configure_planner_submission_turn,
    _configure_smoke_revision_builder,
    _repair_artifact_snapshot,
    _repair_prompt,
    _run_planner,
    _smoke_failure_signature,
    _smoke_revision_retry_prompt,
    planning_candidate_symbols,
    _extract_planning_json,
    _llm_safe_json,
    _planner_prompt,
)
from api.services.files import extract_zip
from fastapi import HTTPException


def _sample_project(root: Path) -> Path:
    project = root / "project"
    project.mkdir()
    (project / "README.md").write_text(
        "# Risk model\nUse predict for scoring and evaluate for labelled batches.\n",
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text("numpy>=1.26\n", encoding="utf-8")
    (project / "core.py").write_text(
        '"""Small algorithm used by the packaging contract tests."""\n\n'
        'def _normalize(value: float) -> float:\n'
        '    return max(0.0, min(1.0, value))\n\n'
        'def predict(value: float) -> dict[str, float]:\n'
        '    """Score one observation."""\n'
        '    return {"score": _normalize(value)}\n\n'
        'def evaluate(values: list[float]) -> dict[str, float]:\n'
        '    """Calculate an average score."""\n'
        '    scores = [predict(value)["score"] for value in values]\n'
        '    return {"mean": sum(scores) / len(scores)}\n',
        encoding="utf-8",
    )
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(
        "from core import predict\n\ndef test_predict():\n    assert predict(0.5)['score'] == 0.5\n",
        encoding="utf-8",
    )
    return project


def _plan(ir) -> PackagingPlan:
    return PackagingPlan.validate(
        {
            "schemaVersion": "ioeb.agentic-mcp-plan/v1",
            "decision": "package",
            "analysisSummary": "仓库提供单条预测与批量评估两种稳定的用户能力。",
            "rejectionReasons": [],
            "services": [
                {
                    "id": "risk_scoring",
                    "name": "Risk scoring",
                    "description": "Score observations and evaluate batches.",
                    "rationale": "Both operations share the same scoring semantics and dependencies.",
                    "tools": [
                        {
                            "name": "predict_risk",
                            "description": "Predict a normalized risk score for one value.",
                            "sourceSymbols": ["core.predict"],
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "number"}},
                                "required": ["value"],
                            },
                            "outputSchema": {
                                "type": "object",
                                "properties": {"score": {"type": "number"}},
                                "required": ["score"],
                            },
                            "adapterStrategy": "Validate a finite number and call core.predict directly.",
                            "dependsOn": [],
                            "smokeTest": {
                                "enabled": True,
                                "input": {"value": 0.5},
                                "evidence": ["tests/test_core.py:4"],
                            },
                            "evidence": ["README.md:2", "core.py:6", "tests/test_core.py:3"],
                        },
                        {
                            "name": "evaluate_risk",
                            "description": "Evaluate the mean score for a batch of values.",
                            "sourceSymbols": ["core.evaluate", "core.predict"],
                            "inputSchema": {
                                "type": "object",
                                "properties": {"values": {"type": "array", "items": {"type": "number"}}},
                                "required": ["values"],
                            },
                            "outputSchema": {
                                "type": "object",
                                "properties": {"mean": {"type": "number"}},
                                "required": ["mean"],
                            },
                            "adapterStrategy": "Validate a non-empty batch and delegate to core.evaluate.",
                            "dependsOn": ["predict_risk"],
                            "smokeTest": {
                                "enabled": True,
                                "input": {"values": [0.2, 0.8]},
                                "evidence": ["README.md:2"],
                            },
                            "evidence": ["README.md:2", "core.py:10"],
                        },
                    ],
                }
            ],
            "excludedSymbols": [{"symbol": "core._normalize", "reason": "internal helper"}],
            "assumptions": [],
            "riskNotes": [],
        },
        known_symbols=ir.known_symbols,
    )


def _valid_server() -> str:
    return '''from mcp.server.fastmcp import FastMCP
from adapters import evaluate_risk as evaluate
from adapters import predict_risk as predict

mcp = FastMCP("Risk scoring", host="0.0.0.0", port=8000, sse_path="/sse", message_path="/messages/")

@mcp.tool()
def predict_risk(value: float) -> dict[str, float]:
    """Predict a normalized risk score for one value."""
    return predict(value)

@mcp.tool()
def evaluate_risk(values: list[float]) -> dict[str, float]:
    """Evaluate the mean score for a batch of values."""
    return evaluate(values)

starlette_app = mcp.sse_app()
'''


def _valid_adapters() -> str:
    return '''from algorithm.core import evaluate as algorithm_evaluate
from algorithm.core import predict as algorithm_predict

def predict_risk(value: float) -> dict[str, float]:
    if not isinstance(value, (int, float)):
        raise ValueError("value must be numeric")
    return algorithm_predict(float(value))

def evaluate_risk(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("values must not be empty")
    return algorithm_evaluate([float(value) for value in values])
'''


def test_repository_analyzer_scans_nested_symbols_and_evidence(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)

    assert {"core.predict", "core.evaluate", "core._normalize"} <= ir.known_symbols
    assert ir.testFiles == ["tests/test_core.py"]
    assert "README.md" in ir.documentation
    assert len(ir.fingerprint) == 64


def test_repository_analyzer_never_truncates_root_template_evidence(tmp_path):
    project = tmp_path / "large"
    project.mkdir()
    (project / "main.py").write_text(
        "def main_process(value: float) -> float:\n    return value\n", encoding="utf-8"
    )
    (project / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (project / "README.md").write_text("# Large repository\n", encoding="utf-8")
    for index in range(20):
        (project / f"a_{index:02}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")

    ir = RepositoryAnalyzer(max_files=3).analyze(project)

    assert [file.path for file in ir.files] == ["main.py", "requirements.txt", "README.md"]
    assert "main.main_process" in ir.known_symbols
    assert "README.md" in ir.documentation
    assert ir.truncated


def test_repository_analyzer_does_not_enumerate_the_full_tree(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "bounded"
    project.mkdir()
    (project / "main.py").write_text(
        "def main_process(value: int) -> int:\n    return value\n",
        encoding="utf-8",
    )
    for directory_index in range(10):
        directory = project / f"package_{directory_index:02d}"
        directory.mkdir()
        for file_index in range(10):
            (directory / f"module_{file_index:02d}.py").write_text(
                f"VALUE = {file_index}\n",
                encoding="utf-8",
            )

    def reject_rglob(*args, **kwargs):
        raise AssertionError("full-tree rglob must not be used")

    monkeypatch.setattr(Path, "rglob", reject_rglob)

    ir = RepositoryAnalyzer(max_files=5).analyze(project)

    assert ir.truncated
    assert len(ir.files) == 5
    assert "main.main_process" in ir.known_symbols


def test_file_path_suggestions_does_not_enumerate_the_full_tree(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "bounded-suggestions"
    (project / "src" / "package").mkdir(parents=True)
    (project / "src" / "package" / "service.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    for directory_index in range(20):
        directory = project / f"vendor_{directory_index:02d}"
        directory.mkdir()
        (directory / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")

    def reject_rglob(*args, **kwargs):
        raise AssertionError("path suggestions must not traverse the complete tree")

    monkeypatch.setattr(Path, "rglob", reject_rglob)

    suggestions = _file_path_suggestions(
        project,
        "package/service.py",
        max_directories=3,
    )

    assert suggestions == ["src/package/service.py"]


def test_darp_propagates_intent_relevance_through_internal_dependencies(tmp_path):
    project = tmp_path / "relevance"
    project.mkdir()
    (project / "main.py").write_text(
        "from predictor import predict_risk\n\n"
        "def main_process(record: dict) -> dict:\n"
        "    return predict_risk(record)\n",
        encoding="utf-8",
    )
    (project / "predictor.py").write_text(
        "from features import normalize_age\n\n"
        "def predict_risk(record: dict) -> dict:\n"
        '    \"\"\"Predict transaction risk from customer features.\"\"\"\n'
        '    return {\"risk\": normalize_age(record[\"age\"])}\n',
        encoding="utf-8",
    )
    (project / "features.py").write_text(
        "def normalize_age(age: int) -> float:\n"
        "    return age / 100\n",
        encoding="utf-8",
    )
    (project / "unrelated.py").write_text(
        "def render_admin_dashboard() -> str:\n"
        '    return \"ok\"\n',
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)

    evidence = build_relevance_evidence(
        ir,
        "Wrap customer transaction risk prediction for compliance agents.",
    )
    scores = {
        path: item["relevance"]
        for path, item in evidence["detailed"].items()
    } | {
        path: item["relevance"]
        for path, item in evidence["compact"].items()
    } | {
        item["path"]: item["relevance"] for item in evidence["minimal"]
    }

    assert scores["main.py"] == 1.0
    assert scores["predictor.py"] >= 0.6
    assert scores["features.py"] >= 0.36
    assert "unrelated.py" not in scores
    assert evidence["overview"]["belowThresholdFileCount"] == 1


def test_analyzer_extracts_literal_dispatch_branches_and_gate_requires_split_tools(tmp_path):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        "from core import evaluate, predict\n\n"
        "def main_process(operation: str, value: float) -> dict:\n"
        "    if operation == 'predict':\n"
        "        return predict(value)\n"
        "    elif operation == 'evaluate':\n"
        "        return evaluate([value])\n"
        "    raise ValueError('unsupported operation')\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    entry = next(
        symbol for symbol in ir.symbols if symbol.qualifiedName == "main.main_process"
    )

    assert [
        (branch["parameter"], branch["value"])
        for branch in entry.dispatchBranches
    ] == [("operation", "evaluate"), ("operation", "predict")]
    raw = _plan(ir).to_dict()
    for tool in raw["services"][0]["tools"]:
        tool["sourceSymbols"] = ["main.main_process"]
        tool["inputSchema"]["properties"]["operation"] = {
            "type": "string",
            "enum": ["predict", "evaluate"],
        }
        tool["adapterStrategy"] = "Pass operation through to main_process."
    generic_plan = PackagingPlan(raw)

    errors = assess_dispatch_coverage(
        generic_plan, {"main.main_process": entry.dispatchBranches}
    )

    assert any("仍把分派参数暴露" in error for error in errors)
    assert any("未被独立 Tool 覆盖" in error for error in errors)

    for tool, value in zip(raw["services"][0]["tools"], ("predict", "evaluate")):
        tool["inputSchema"]["properties"].pop("operation")
        tool["adapterStrategy"] = (
            f"Validate public inputs, set operation='{value}', and call main_process."
        )
    split_plan = PackagingPlan(raw)

    assert not assess_dispatch_coverage(
        split_plan, {"main.main_process": entry.dispatchBranches}
    )
    assert not assess_dispatch_coverage(
        generic_plan,
        {
            "main.main_process": [
                {"parameter": "model_name", "value": "small", "line": 1, "calls": []},
                {"parameter": "model_name", "value": "large", "line": 2, "calls": []},
            ]
        },
    )


async def test_plan_store_applies_dispatch_coverage_gate(tmp_path):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        "from core import evaluate, predict\n\n"
        "def main_process(operation: str, value: float) -> dict:\n"
        "    if operation == 'predict':\n"
        "        return predict(value)\n"
        "    if operation == 'evaluate':\n"
        "        return evaluate([value])\n"
        "    raise ValueError('unsupported operation')\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    entry = next(
        symbol for symbol in ir.symbols if symbol.qualifiedName == "main.main_process"
    )
    raw = _plan(ir).to_dict()
    for tool in raw["services"][0]["tools"]:
        tool["sourceSymbols"] = ["main.main_process"]
        tool["adapterStrategy"] = "Pass the selected operation through to main_process."
    store = PlanStore(
        tmp_path / "plan.json",
        ir.known_symbols,
        symbol_dispatch_branches={"main.main_process": entry.dispatchBranches},
    )

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert result.error
    assert store.plan is None
    assert "分支能力覆盖门禁失败" in result.error


async def test_runtime_smoke_revision_preserves_reviewed_interface(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        enforce_interface_quality=False,
    )
    result = await ReviseSmokeTests(store, plan).execute(
        revisions=[
            {
                "toolName": plan.tool_names[0],
                "input": {"value": 0.7},
                "evidence": ["tests/test_core.py:4"],
            }
        ],
    )

    assert not result.error
    assert store.plan is not None
    assert store.plan.tools[0]["smokeTest"]["input"] == {"value": 0.7}
    assert store.plan.tools[0]["smokeTest"]["evidence"] == ["tests/test_core.py:4"]
    assert store.plan.tools[0]["description"] == plan.tools[0]["description"]

    rejected_store = PlanStore(
        path=tmp_path / "rejected_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        enforce_interface_quality=False,
    )
    rejected = await ReviseSmokeTests(rejected_store, plan).execute(
        revisions=[
            {
                "toolName": "hallucinated_tool",
                "input": {"value": 0.9},
                "evidence": ["tests/test_core.py:4"],
            }
        ],
    )

    assert rejected.error
    assert "未知 Tool" in rejected.error
    assert rejected_store.plan is None


async def test_runtime_smoke_revision_rejects_noop_and_unknown_fields(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        enforce_interface_quality=False,
    )
    original = plan.tools[0]["smokeTest"]

    noop = await ReviseSmokeTests(store, plan).execute(
        revisions=[
            {
                "toolName": plan.tool_names[0],
                "input": original["input"],
                "evidence": original["evidence"],
            }
        ],
    )
    assert noop.error
    assert "完全相同" in noop.error

    unknown_field = await ReviseSmokeTests(store, plan).execute(
        revisions=[
            {
                "toolName": plan.tool_names[0],
                "input": {"value": 0.9},
                "evidence": ["tests/test_core.py:4"],
                "description": "must not be accepted",
            }
        ],
    )
    assert unknown_field.error
    assert "不允许字段" in unknown_field.error


async def test_runtime_smoke_revision_keeps_valid_subset_when_peer_is_invalid(
    tmp_path,
):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        enforce_interface_quality=False,
    )

    result = await ReviseSmokeTests(store, plan).execute(
        revisions=[
            {
                "toolName": plan.tool_names[0],
                "input": {"value": 0.7},
                "evidence": ["tests/test_core.py:4"],
            },
            {
                "toolName": plan.tool_names[1],
                "input": {"unknown": 0.1},
                "evidence": ["missing_fixture.py:1"],
            },
        ],
    )

    assert not result.error
    assert "其余修订未应用" in result.output
    assert store.plan is not None
    assert store.plan.tools[0]["smokeTest"]["input"] == {"value": 0.7}
    assert store.plan.tools[1]["smokeTest"] == plan.tools[1]["smokeTest"]


async def test_runtime_smoke_revision_parses_stringified_revision_array(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        enforce_interface_quality=False,
    )
    revisions = [
        {
            "toolName": plan.tool_names[0],
            "input": {"value": 0.7},
            "evidence": ["tests/test_core.py:4"],
        }
    ]

    result = await ReviseSmokeTests(store, plan).execute(
        revisions=json.dumps(revisions),
    )

    assert not result.error
    assert store.plan is not None
    assert store.plan.tools[0]["smokeTest"]["input"] == {"value": 0.7}


async def test_runtime_smoke_revision_auto_grounds_free_text_from_repository(
    tmp_path,
):
    project = _sample_project(tmp_path)
    examples = project / "examples"
    examples.mkdir()
    (examples / "fixture.py").write_text(
        'SCENARIO = "documented risk fixture"\n',
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    tool = raw["services"][0]["tools"][0]
    tool["inputSchema"]["properties"]["scenario"] = {
        "type": "string",
        "description": "Named source-backed scenario.",
    }
    tool["inputSchema"]["required"].append("scenario")
    tool["smokeTest"]["input"]["scenario"] = "documented risk fixture"
    tool["smokeTest"]["evidence"] = ["examples/fixture.py:1"]
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        enforce_interface_quality=False,
        require_independent_smoke_evidence=True,
        smoke_evidence_root=project,
    )

    result = await ReviseSmokeTests(store, plan).execute(
        revisions=[
            {
                "toolName": plan.tool_names[0],
                "input": {
                    "value": 0.7,
                    "scenario": "invented placeholder",
                },
                "evidence": ["README.md:1"],
            }
        ],
    )

    assert not result.error
    assert "机械替换" in result.output
    assert store.plan is not None
    revised = store.plan.tools[0]["smokeTest"]
    assert revised["input"]["scenario"] == "documented risk fixture"
    assert revised["evidence"] == ["examples/fixture.py:1"]


async def test_runtime_smoke_revision_never_reuses_container_failed_input(
    tmp_path,
):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    failed_input = {"value": 0.7}
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        enforce_interface_quality=False,
        rejected_smoke_inputs={
            plan.tool_names[0]: {_canonical_smoke_input(failed_input)}
        },
    )

    result = await ReviseSmokeTests(store, plan).execute(
        revisions=[
            {
                "toolName": plan.tool_names[0],
                "input": failed_input,
                "evidence": ["tests/test_core.py:4"],
            }
        ],
    )

    assert result.error
    assert "隔离容器实际执行并判定失败" in result.error
    assert store.plan is None
    assert store.last_errors
    assert store.smoke_revision_attempted


def test_smoke_revision_retry_prompt_forbids_failed_complete_inputs():
    failed = {"simulate": {"{\"reaction\":\"A -> B\"}"}}

    prompt = _smoke_revision_retry_prompt(
        ["fixture lacks independent evidence"],
        failed,
    )

    assert '"reaction": "A -> B"' in prompt
    assert "禁止回退" in prompt
    assert "不能只机械替换一个自由文本字段" in prompt


def test_smoke_fixture_is_frozen_only_with_exact_independent_provenance():
    generic = [
        "[smoke_evidence_reference] predict.smokeTest.evidence 只引用了生成的 main.py"
    ]
    grounded = [
        "[smoke_evidence_reference] predict.smokeTest.input 引用错误；"
        "当前 input 已在候选独立证据中逐字出现，必须保持 input 不变并仅更新 evidence"
    ]

    assert not _smoke_errors_prove_fixture_grounding(generic)
    assert _smoke_errors_prove_fixture_grounding(grounded)


def test_bage_retains_budgeted_inventory_without_promoting_unrelated_files(tmp_path):
    project = tmp_path / "budget"
    project.mkdir()
    (project / "main.py").write_text(
        "from domain import run\n\ndef main() -> dict:\n    return run()\n",
        encoding="utf-8",
    )
    (project / "domain.py").write_text(
        "def run() -> dict:\n"
        '    \"\"\"Run the documented domain algorithm.\"\"\"\n'
        '    return {\"status\": \"ok\"}\n',
        encoding="utf-8",
    )
    for index in range(12):
        (project / f"unused_{index}.py").write_text(
            f"def helper_{index}() -> int:\n    return {index}\n",
            encoding="utf-8",
        )
    ir = RepositoryAnalyzer().analyze(project)

    evidence = build_relevance_evidence(ir, "Run the domain algorithm.", max_tokens=900)

    assert evidence["overview"]["estimatedTokens"] <= 900
    assert evidence["overview"]["relevantFiles"] == 2
    assert evidence["overview"]["belowThresholdFileCount"] == 12
    assert "main.py" in evidence["detailed"]
    assert "domain.py" in evidence["detailed"] or "domain.py" in evidence["compact"]


def test_template_main_process_is_planning_audit_boundary_not_only_possible_source(tmp_path):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        '''from core import evaluate, predict

def main_process(operation: str, value: float) -> dict[str, float]:
    """Dispatch the supported algorithm operations.

    Args:
        operation: Operation name.
        value: Input value.

    Returns:
        Algorithm result.
    """
    if operation == "predict":
        return predict(value)
    return evaluate([value])
''',
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)

    assert planning_candidate_symbols(ir) == {"main.main_process"}
    workflow = AgenticAnalysisWorkflow(
        project_dir=project,
        ir=ir,
        graph_path=tmp_path / "function.json",
    )
    assert workflow.plan_store.candidate_symbols == {"main.main_process"}
    assert workflow.agent.tools.get("read_project_file").max_reads == 10
    assert {"core.predict", "core.evaluate"} <= ir.known_symbols
    packaging = AgenticPackagingWorkflow(
        project_dir=project,
        ir=ir,
        artifact_dir=tmp_path / "artifact",
    )
    assert packaging.max_repairs == 4
    assert packaging.max_runtime_repairs == 8


def test_repair_builder_has_bounded_evidence_and_patch_only_tools(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    builder = _build_builder_agent(project, tmp_path / "artifact", plan, ir)

    assert builder.tools.get("inspect_repository") is None
    assert builder.tools.get("read_project_file").max_reads == 6

    _configure_repair_builder(builder)

    assert builder.tools.get("inspect_repository") is None
    assert builder.tools.get("read_project_file").max_reads == 1
    assert builder.tools.get("write_artifact_file").allow_nonempty_overwrite is False


def test_repeated_smoke_failure_routes_to_fixture_only_agent(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = tmp_path / "artifact"
    builder = _build_builder_agent(project, artifact, plan, ir)
    store = PlanStore(
        path=artifact / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )

    _configure_repair_builder(builder)
    _configure_smoke_revision_builder(
        builder,
        store,
        plan,
        force_revision=True,
    )

    assert set(builder.tools.list_names()) == {
        "read_project_file",
        "revise_smoke_tests",
        "terminate",
    }
    assert builder.max_steps == 16
    assert "专用 fixture 修订" in builder.system_prompt

    report = VerificationReport(
        passed=False,
        checks={"smokeTestFailures": {"predict_risk": "invalid fixture"}},
        errors=["[smoke_test] failed"],
    )
    assert _smoke_failure_signature(report) == json.dumps(
        {"predict_risk": "invalid fixture"},
        ensure_ascii=False,
        sort_keys=True,
    )


def test_planner_submission_turn_reuses_evidence_with_only_save_tool(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )
    planner = _build_planning_agent(project, ir, store)

    configured = _configure_planner_submission_turn(planner)

    assert configured
    assert set(planner.tools.list_names()) == {"save_packaging_plan_json"}
    assert planner.terminal_tools == {"save_packaging_plan_json"}
    assert planner.max_steps == 4
    assert "立即调用 save_packaging_plan_json" in planner.next_step_prompt


def test_builder_context_passes_bounded_reviewed_source_artifacts(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)

    context = _builder_implementation_context(
        plan,
        ir,
        max_total_chars=300,
        max_file_chars=300,
    )

    assert context["packagingPlan"] == plan.to_dict()
    assert {"core.predict", "core.evaluate"} <= set(context["sourceSymbols"])
    assert "core.py" in context["sourceExcerpts"]
    assert sum(len(value) for value in context["sourceExcerpts"].values()) <= 300
    assert "def predict" in context["sourceExcerpts"]["core.py"]


def test_large_verified_fixtures_are_summarized_only_in_llm_prompts(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    signal = [float(index) for index in range(20_000)]
    context = _builder_implementation_context(plan, ir)
    context["verifiedTemplateContract"] = {
        "runtimePassed": True,
        "records": [{"toolSmokeInput": {"signal": signal}}],
    }

    safe = _llm_safe_json(context)
    prompt = _builder_prompt(plan, ir, context)

    assert context["verifiedTemplateContract"]["records"][0][
        "toolSmokeInput"
    ]["signal"] == signal
    summary = safe["verifiedTemplateContract"]["records"][0][
        "toolSmokeInput"
    ]["signal"]["$ioebLargeValue"]
    assert summary["length"] == 20_000
    assert summary["restoredBySystem"] is True
    assert len(prompt) < 50_000
    assert "$ioebLargeValue" in prompt
    assert '"length": 20000' in prompt


def test_planner_prioritizes_submitted_template_contract_tests(tmp_path):
    project = _sample_project(tmp_path)
    contract_tests = project / "tests_ioeb"
    contract_tests.mkdir()
    (contract_tests / "test_template_contract.py").write_text(
        "def test_contract():\n    assert True\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)

    prompt = _planner_prompt(ir, "wrap the submitted contract")

    assert '"templateContractEvidenceFiles": [' in prompt
    assert "tests_ioeb/test_template_contract.py" in prompt
    assert "必须在上游库内部单元测试之前优先读取" in prompt


def test_analyzer_keeps_contract_evidence_when_inventory_is_truncated(tmp_path):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        "def main_process(value: float) -> dict:\n"
        "    return {'value': value}\n",
        encoding="utf-8",
    )
    (project / "template_adaptation.json").write_text(
        '{"contractRuntime":{"passed":true}}',
        encoding="utf-8",
    )
    contract_dir = project / "tests_ioeb"
    contract_dir.mkdir()
    (contract_dir / "test_template_contract.py").write_text(
        "from main import main_process\n"
        "def test_contract():\n"
        "    assert main_process(1.0)['value'] == 1.0\n",
        encoding="utf-8",
    )
    for index in range(20):
        (project / f"module_{index:02d}.py").write_text(
            f"VALUE = {index}\n",
            encoding="utf-8",
        )

    ir = RepositoryAnalyzer(max_files=3).analyze(project)
    paths = {file.path for file in ir.files}

    assert ir.truncated
    assert paths == {
        "main.py",
        "template_adaptation.json",
        "tests_ioeb/test_template_contract.py",
    }


def test_planner_embeds_verified_contract_fixture_by_dispatch_branch(tmp_path):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        '''from core import evaluate, predict

def main_process(
    operation: str,
    output_type: str,
    model_variant: str,
    value: float,
) -> dict[str, float]:
    """Dispatch a repository operation.

    Args:
        operation: Operation name.
        output_type: Agent-visible output capability.
        model_variant: Non-capability model configuration.
        value: Input value.

    Returns:
        Repository result.
    """
    if model_variant == "double":
        value *= 2
    elif model_variant != "plain":
        raise ValueError("unsupported model variant")
    if output_type == "structured":
        pass
    elif output_type == "summary":
        value = round(value, 2)
    else:
        raise ValueError("unsupported output type")
    if operation == "predict":
        return predict(value)
    if operation == "evaluate":
        return evaluate([value])
    raise ValueError("unsupported")
''',
        encoding="utf-8",
    )
    contract_tests = project / "tests_ioeb"
    contract_tests.mkdir()
    (contract_tests / "test_template_contract.py").write_text(
        '''import pytest
from main import main_process

def test_predict_contract():
    result = main_process(
        operation="predict",
        output_type="structured",
        model_variant="plain",
        value=0.5,
    )
    assert result["score"] == 0.5

def test_invalid_contract():
    with pytest.raises(ValueError):
        main_process(
            operation="invalid",
            output_type="structured",
            model_variant="plain",
            value=0.5,
        )
''',
        encoding="utf-8",
    )
    (project / "template_adaptation.json").write_text(
        json.dumps(
            {
                "validation": {
                    "passed": True,
                    "checks": {
                        "contractFixtures": [
                            {
                                "line": 5,
                                "input": {
                                    "operation": "predict",
                                    "output_type": "structured",
                                    "model_variant": "plain",
                                    "value": 0.5,
                                },
                            },
                            {
                                "line": 15,
                                "input": {
                                    "operation": "invalid",
                                    "output_type": "structured",
                                    "model_variant": "plain",
                                    "value": 0.5,
                                },
                            },
                        ]
                    },
                },
                "contractRuntime": {
                    "passed": True,
                    "errors": [],
                    "warnings": [],
                    "checks": {
                        "functionalVerified": True,
                        "executionMode": "repository_source",
                        "networkDuringTest": False,
                        "contractFixtures": [
                            {
                                "line": 5,
                                "input": {
                                    "operation": "predict",
                                    "output_type": "structured",
                                    "model_variant": "plain",
                                    "value": 0.75,
                                },
                                "expectedOutcome": "success",
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)

    prompt = _planner_prompt(ir, "wrap the submitted contract")
    relevance = build_relevance_evidence(ir, "wrap the submitted contract")

    assert '"runtimePassed": true' in prompt
    assert '"dispatchBindings": [' in prompt
    assert '"parameter": "operation"' in prompt
    assert '"value": "predict"' in prompt
    assert '"parameter": "output_type"' in prompt
    assert '"value": "structured"' in prompt
    assert '"toolSmokeInput": {' in prompt
    assert '"value": 0.75' in prompt
    assert "tests_ioeb/test_template_contract.py:5" in prompt
    assert "保持 toolSmokeInput 中的值不变" in prompt
    context = _builder_implementation_context(_plan(ir), ir)
    assert context["verifiedTemplateContract"]["runtimePassed"] is True
    assert context["verifiedTemplateContract"]["records"][0][
        "mainProcessInput"
    ] == {
        "operation": "predict",
        "output_type": "structured",
        "model_variant": "plain",
        "value": 0.75,
    }
    assert context["verifiedTemplateContract"]["records"][0][
        "toolSmokeInput"
    ] == {"model_variant": "plain", "value": 0.75}
    assert len(context["verifiedTemplateContract"]["records"]) == 1
    assert context["verifiedTemplateContract"]["excludedErrorFixtureCount"] == 0
    assert "tests_ioeb/test_template_contract.py" in {
        item["path"] for item in relevance["overview"]["seedFiles"]
    }


async def test_plan_store_deterministically_grounds_verified_contract_smoke(
    tmp_path,
):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        "from core import predict\n\n"
        "def main_process(operation: str, value: float, context: list[str]) -> dict:\n"
        "    if operation == 'predict':\n"
        "        return {'score': predict(value), 'context': context}\n"
        "    raise ValueError('unsupported')\n",
        encoding="utf-8",
    )
    contract_dir = project / "tests_ioeb"
    contract_dir.mkdir()
    contract_file = contract_dir / "test_template_contract.py"
    contract_file.write_text(
        "from main import main_process\n\n"
        "def test_predict_contract():\n"
        "    result = main_process(operation='predict', value=0.5, context=['baseline'])\n"
        "    assert result['score'] == 0.5\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"] = [raw["services"][0]["tools"][0]]
    tool = raw["services"][0]["tools"][0]
    tool["sourceSymbols"] = ["main.main_process"]
    tool["adapterStrategy"] = (
        "Validate value and context, set operation='predict' and "
        "output_type='structured', then call main.main_process."
    )
    tool["smokeTest"] = {
        "enabled": True,
        "input": {"value": 999.0},
        "evidence": ["main.py:4"],
    }
    store = PlanStore(
        path=tmp_path / "plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        symbol_required_parameters={
            "main.main_process": ["operation", "value", "context"]
        },
        symbol_dispatch_branches={
            "main.main_process": next(
                symbol.dispatchBranches
                for symbol in ir.symbols
                if symbol.qualifiedName == "main.main_process"
            )
        },
        require_independent_smoke_evidence=True,
        smoke_evidence_root=project,
        verified_contract_records=[
            {
                "dispatchBindings": [
                    {"parameter": "operation", "value": "predict"},
                    {"parameter": "output_type", "value": "structured"},
                ],
                "dispatchParameter": None,
                "dispatchValue": None,
                "mainProcessInput": {
                    "operation": "predict",
                    "output_type": "structured",
                    "value": 0.5,
                    "context": ["baseline"],
                },
                "toolSmokeInput": {
                    "value": 0.5,
                    "context": ["baseline"],
                },
                "evidence": ["tests_ioeb/test_template_contract.py:4"],
            }
        ],
    )

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert not result.error
    assert store.plan is not None
    assert store.plan.tools[0]["smokeTest"] == {
        "enabled": True,
        "input": {"value": 0.5, "context": ["baseline"]},
        "evidence": ["tests_ioeb/test_template_contract.py:4"],
    }
    grounded_schema = store.plan.tools[0]["inputSchema"]
    assert grounded_schema["properties"]["context"]["type"] == "array"
    assert grounded_schema["properties"]["context"]["items"] == {
        "type": "string"
    }
    assert "context" in grounded_schema["required"]
    assert store.contract_smoke_grounded_tools == ["predict_risk"]
    assert "verifiedContractSmoke=predict_risk" in result.output


async def test_plan_store_never_regrounds_runtime_rejected_contract_smoke(
    tmp_path,
):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        "from core import predict\n\n"
        "def main_process(operation: str, value: float) -> dict:\n"
        "    if operation == 'predict':\n"
        "        return predict(value)\n"
        "    raise ValueError('unsupported')\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"] = [raw["services"][0]["tools"][0]]
    tool = raw["services"][0]["tools"][0]
    tool["sourceSymbols"] = ["main.main_process"]
    tool["adapterStrategy"] = (
        "Validate value, set operation='predict', and call main.main_process."
    )
    tool["smokeTest"] = {
        "enabled": True,
        "input": {"value": 0.7},
        "evidence": ["tests/test_core.py:4"],
    }
    rejected = {"value": 0.5}
    store = PlanStore(
        path=tmp_path / "plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        rejected_smoke_inputs={
            "predict_risk": {_canonical_smoke_input(rejected)}
        },
        verified_contract_records=[
            {
                "dispatchParameter": "operation",
                "dispatchValue": "predict",
                "mainProcessInput": {"operation": "predict", **rejected},
                "toolSmokeInput": rejected,
                "evidence": ["tests_ioeb/test_template_contract.py:4"],
            }
        ],
    )

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert not result.error
    assert store.plan is not None
    assert store.plan.tools[0]["smokeTest"]["input"] == {"value": 0.7}
    assert store.contract_smoke_grounded_tools == []


async def test_verified_contract_gate_rejects_schema_that_cannot_carry_fixture(
    tmp_path,
):
    project = _sample_project(tmp_path)
    (project / "main.py").write_text(
        "from core import predict\n\n"
        "def main_process(operation: str, value: float) -> dict:\n"
        "    if operation == 'predict':\n"
        "        return predict(value)\n"
        "    raise ValueError('unsupported')\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"] = [raw["services"][0]["tools"][0]]
    tool = raw["services"][0]["tools"][0]
    tool["sourceSymbols"] = ["main.main_process"]
    tool["adapterStrategy"] = (
        "Validate score_value, set operation='predict', and call main.main_process."
    )
    tool["inputSchema"] = {
        "type": "object",
        "properties": {"score_value": {"type": "number"}},
        "required": ["score_value"],
    }
    tool["smokeTest"] = {
        "enabled": True,
        "input": {"score_value": 0.5},
        "evidence": ["tests/test_core.py:4"],
    }
    store = PlanStore(
        path=tmp_path / "plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        verified_contract_records=[
            {
                "dispatchParameter": "operation",
                "dispatchValue": "predict",
                "mainProcessInput": {"operation": "predict", "value": 0.5},
                "toolSmokeInput": {"value": 0.5},
                "evidence": ["tests_ioeb/test_template_contract.py:4"],
            }
        ],
    )

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert result.error
    assert "已验证模板契约对齐门禁失败" in result.error
    assert "[verified_contract_schema]" in result.error
    assert "契约字段=['value']" in result.error
    assert store.plan is None


async def test_verified_contract_gate_rejects_bypassing_public_entrypoint(
    tmp_path,
):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"] = [raw["services"][0]["tools"][0]]
    store = PlanStore(
        path=tmp_path / "plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
        verified_contract_records=[
            {
                "dispatchParameter": None,
                "dispatchValue": None,
                "mainProcessInput": {"value": 0.5},
                "toolSmokeInput": {"value": 0.5},
                "evidence": ["tests_ioeb/test_template_contract.py:4"],
            }
        ],
    )

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert result.error
    assert "[verified_contract_source]" in result.error
    assert "不能绕过公共契约" in result.error
    assert store.plan is None


async def test_project_reader_corrects_artifact_prefixed_source_path(tmp_path):
    project = _sample_project(tmp_path)

    result = await ReadProjectFile(project).execute(path="algorithm/core.py")

    assert result.error
    assert "请改用 core.py" in result.error


async def test_project_reader_exhaustion_requires_current_stage_completion(tmp_path):
    project = _sample_project(tmp_path)
    reader = ReadProjectFile(project, max_reads=1)

    first = await reader.execute(path="core.py")
    exhausted = await reader.execute(path="core.py")

    assert not first.error
    assert exhausted.error
    assert "不得再次调用 read_project_file" in exhausted.error
    assert "完成当前阶段要求的规划或产物" in exhausted.error
    assert "结构化规划" not in exhausted.error


async def test_project_reader_invalid_path_does_not_consume_read_budget(tmp_path):
    project = _sample_project(tmp_path)
    reader = ReadProjectFile(project, max_reads=1)

    invalid = await reader.execute(path="algorithm/")
    valid = await reader.execute(path="core.py")
    exhausted = await reader.execute(path="README.md")

    assert invalid.error
    assert "真实文件或目录路径" in invalid.error
    assert not valid.error
    assert exhausted.error


async def test_project_reader_lists_one_directory_level_and_spends_budget(
    tmp_path,
):
    project = _sample_project(tmp_path)
    assets = project / "assets"
    (assets / "models").mkdir(parents=True)
    (assets / "weights.bin").write_bytes(b"weights")
    reader = ReadProjectFile(project, max_reads=1)

    listing = await reader.execute(path="assets")
    exhausted = await reader.execute(path="core.py")

    assert not listing.error
    assert "# Directory assets" in listing.output
    assert "models/" in listing.output
    assert "weights.bin" in listing.output
    assert exhausted.error


async def test_project_reader_suggests_src_layout_without_spending_budget(
    tmp_path,
):
    project = _sample_project(tmp_path)
    nested = project / "src" / "package"
    nested.mkdir(parents=True)
    (nested / "model.py").write_text(
        "def predict(value):\n    return value\n",
        encoding="utf-8",
    )
    reader = ReadProjectFile(project, max_reads=1)

    missing = await reader.execute(path="package/model.py")
    corrected = await reader.execute(path="src/package/model.py")
    exhausted = await reader.execute(path="core.py")

    assert missing.error
    assert "src/package/model.py" in missing.error
    assert "不要继续重复不存在的路径" in missing.error
    assert not corrected.error
    assert exhausted.error


def test_non_template_repository_retains_full_public_symbol_audit(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))

    assert planning_candidate_symbols(ir) == ir.public_callable_symbols
    assert {"core.predict", "core.evaluate"} <= planning_candidate_symbols(ir)


def test_plan_generates_multi_tool_legacy_graph(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    plan = _plan(ir)
    graph = plan.to_frontend_graph()

    assert [node["label"] for node in graph["nodes"]] == ["predict_risk", "evaluate_risk"]
    assert graph["edges"] == [{"sourceID": "9001", "targetID": "9002"}]
    assert graph["meta"] == {
        "schemaVersion": "ioeb.agentic-mcp-plan/v1",
        "engine": "agentic",
        "serviceCount": 1,
        "toolCount": 2,
        "analysisSummary": "仓库提供单条预测与批量评估两种稳定的用户能力。",
    }


def test_reference_free_interface_quality_gate_rejects_lossy_contract(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))

    report = assess_interface_quality(_plan(ir))

    assert not report.passed
    assert report.metrics["parameterDescriptionCoverage"] == 0
    assert report.metrics["outputDescriptionCoverage"] == 0
    assert any("MCP 参数缺少" in error for error in report.errors)
    assert any("输出字段缺少" in error for error in report.errors)


def test_reference_free_interface_quality_gate_accepts_evidence_rich_contract(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    descriptions = {
        "predict_risk": (
            "Predict a normalized risk score for one observation when an agent "
            "needs a bounded value for downstream comparison and decision making."
        ),
        "evaluate_risk": (
            "Evaluate the mean normalized risk across multiple observations when "
            "an agent needs an aggregate batch assessment instead of one prediction."
        ),
    }
    for tool in raw["services"][0]["tools"]:
        tool["description"] = descriptions[tool["name"]]
        for name, schema in tool["inputSchema"]["properties"].items():
            schema["description"] = f"Validated {name} input documented by the algorithm contract."
        for name, schema in tool["outputSchema"]["properties"].items():
            schema["description"] = f"Structured {name} result produced by the algorithm."
    raw["services"][0]["tools"][0]["inputSchema"]["properties"]["value"].update(
        {"minimum": 0, "maximum": 1}
    )
    raw["services"][0]["tools"][1]["inputSchema"]["properties"]["values"].update(
        {"minItems": 1}
    )
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)

    report = assess_interface_quality(plan)

    assert report.passed, report.to_json()
    assert report.metrics["parameterDescriptionCoverage"] == 1
    assert report.metrics["outputDescriptionCoverage"] == 1
    assert report.metrics["referenceFreeGoE"] >= 0.72


def test_reference_free_interface_quality_gate_rejects_required_defaults(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    tool = raw["services"][0]["tools"][0]
    tool["inputSchema"]["properties"]["value"]["default"] = 0.5
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)

    report = assess_interface_quality(plan, min_goe=0)

    assert not report.passed
    assert any(
        "predict_risk 同时把参数声明为 required 和 default: value" in error
        for error in report.errors
    )


def test_reference_free_interface_quality_gate_rejects_dispatcher_envelopes(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    tool = raw["services"][0]["tools"][0]
    tool["description"] = (
        "Predict a normalized observation risk score for downstream decisions "
        "using the repository's bounded scoring algorithm."
    )
    tool["inputSchema"]["properties"]["value"].update(
        {
            "description": "Observation value accepted by the risk scoring algorithm.",
            "minimum": 0,
            "maximum": 1,
        }
    )
    tool["outputSchema"] = {
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "Whether dispatch succeeded."},
            "operation": {"type": "string", "description": "Fixed dispatcher operation."},
            "result": {"type": "object", "description": "Generic dispatcher payload."},
            "error": {"type": "string", "description": "Dispatcher failure detail."},
        },
        "required": ["success", "operation", "result"],
    }
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)

    report = assess_interface_quality(plan)

    assert not report.passed
    assert any("控制信封" in error and "predict_risk" in error for error in report.errors)


def test_reference_free_interface_quality_gate_rejects_required_selector_outputs(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    tool = raw["services"][0]["tools"][0]
    tool["description"] = (
        "Calculate selected risk metrics for an observation when a caller needs "
        "a configurable subset of interpretable scoring outputs."
    )
    tool["inputSchema"] = {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "Observation value accepted by the metric calculator.",
            },
            "metrics": {
                "type": "array",
                "items": {"type": "string", "enum": ["score", "confidence"]},
                "description": "Metric names to calculate and return.",
                "default": ["score"],
            },
        },
        "required": ["value"],
    }
    tool["outputSchema"] = {
        "type": "object",
        "description": "Mapping of requested metric names to numeric values.",
        "properties": {
            "score": {"type": "number", "description": "Normalized risk score."},
            "confidence": {"type": "number", "description": "Score confidence."},
        },
        "required": ["score", "confidence"],
    }
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)

    report = assess_interface_quality(plan)

    assert not report.passed
    assert any(
        "metrics" in error and "条件字段声明为必返" in error
        for error in report.errors
    )


def test_reference_free_interface_quality_gate_rejects_mechanical_service_splits(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    predict, evaluate = raw["services"][0]["tools"]
    predict["sourceSymbols"].append("core.evaluate")
    evaluate["sourceSymbols"].append("core.predict")
    raw["services"] = [
        {
            "id": "prediction_service",
            "name": "Prediction service",
            "description": "Prediction service",
            "rationale": (
                "These tools share the same algorithm contract, runtime dependencies, "
                "and lifecycle."
            ),
            "tools": [predict],
        },
        {
            "id": "evaluation_service",
            "name": "Evaluation service",
            "description": "Evaluation service",
            "rationale": (
                "These tools share the same algorithm contract, runtime dependencies, "
                "and lifecycle."
            ),
            "tools": [evaluate],
        },
    ]
    for tool in (predict, evaluate):
        tool["description"] = (
            f"{tool['description']} Use this operation for its distinct audited "
            "algorithm workflow and structured result contract."
        )
        for name, schema in tool["inputSchema"]["properties"].items():
            schema["description"] = f"Validated {name} input for the audited operation."
        for name, schema in tool["outputSchema"]["properties"].items():
            schema["description"] = f"Structured {name} produced by the audited operation."
    predict["inputSchema"]["properties"]["value"]["minimum"] = 0
    evaluate["inputSchema"]["properties"]["values"]["minItems"] = 1
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)

    report = assess_interface_quality(plan)

    assert not report.passed
    assert report.metrics["serviceCount"] == 2
    assert report.metrics["crossServiceSharedSourcePairs"] == 1
    assert any("跨服务工具共享同一源码入口" in error for error in report.errors)
    assert any("description 只是重复服务名称" in error for error in report.errors)
    assert any("完全相同的边界理由" in error for error in report.errors)


def test_plan_rejects_unknown_source_symbol(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["sourceSymbols"] = ["missing.predict"]

    try:
        PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    except PlanValidationError as exc:
        assert "未知符号" in str(exc)
    else:
        raise AssertionError("unknown source symbol should fail validation")


async def test_plan_tool_suggests_real_symbols_for_unknown_source_reference(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["sourceSymbols"] = ["core.predcit"]

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert result.error
    assert "core.predict" in result.error
    assert "必须核对源码后选择" in result.error
    assert store.plan is None


def test_plan_rejects_server_paths_and_duplicate_semantic_tools(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["inputSchema"]["properties"]["data_path"] = {"type": "string"}
    duplicate = json.loads(json.dumps(raw["services"][0]["tools"][0]))
    duplicate["name"] = "predict_risk_copy"
    raw["services"][0]["tools"].append(duplicate)

    try:
        PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    except PlanValidationError as exc:
        assert "容器文件系统" in str(exc)
        assert "完全相同" in str(exc)
    else:
        raise AssertionError("server path and duplicate surface should fail validation")


def test_plan_rejects_insufficient_direct_source_parameters(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][1]["sourceSymbols"] = ["core.evaluate"]
    raw["services"][0]["tools"][1]["inputSchema"] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    try:
        PackagingPlan.validate(
            raw,
            known_symbols=ir.known_symbols,
            symbol_required_parameters={
                symbol.qualifiedName: symbol.requiredParameters for symbol in ir.symbols
            },
        )
    except PlanValidationError as exc:
        assert "源码必填参数" in str(exc)
        assert "`values=None`" in str(exc)
    else:
        raise AssertionError("missing direct source parameters should fail validation")


def test_plan_accepts_explicitly_derived_source_parameters(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    tool = raw["services"][0]["tools"][1]
    tool["sourceSymbols"] = ["core.evaluate"]
    tool["inputSchema"] = {
        "type": "object",
        "properties": {"values_json": {"type": "string"}},
        "required": ["values_json"],
    }
    tool["adapterStrategy"] = "Parse values_json into the required values list, then call core.evaluate."
    tool["smokeTest"] = {
        "enabled": False,
        "rationale": "No repository fixture uses the adapted JSON transport.",
    }

    plan = PackagingPlan.validate(
        raw,
        known_symbols=ir.known_symbols,
        symbol_required_parameters={
            symbol.qualifiedName: symbol.requiredParameters for symbol in ir.symbols
        },
    )

    assert plan.tools[1]["inputSchema"]["required"] == ["values_json"]


def test_plan_accepts_fixed_literal_positional_source_parameter(tmp_path):
    project = tmp_path / "dispatch"
    project.mkdir()
    (project / "main.py").write_text(
        "def main_process(smiles: str, operation: str) -> dict:\n"
        "    return {'smiles': smiles, 'operation': operation}\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    raw = {
        "schemaVersion": "ioeb.agentic-mcp-plan/v1",
        "decision": "package",
        "analysisSummary": "将稳定的分子相似度分支抽象为单独工具。",
        "services": [
            {
                "id": "molecule",
                "name": "Molecule",
                "description": "Molecular operations.",
                "rationale": "One cohesive algorithm boundary.",
                "tools": [
                    {
                        "name": "parse_molecule",
                        "description": "Parse a molecule from a SMILES string.",
                        "sourceSymbols": ["main.main_process"],
                        "inputSchema": {
                            "type": "object",
                            "properties": {"smiles": {"type": "string"}},
                            "required": ["smiles"],
                        },
                        "outputSchema": {"type": "object"},
                        "adapterStrategy": (
                            "Call main_process(smiles, 'parse') and serialize its result."
                        ),
                        "dependsOn": [],
                        "smokeTest": {
                            "enabled": False,
                            "rationale": "No repository fixture is available.",
                        },
                        "evidence": ["main.py:1"],
                    }
                ],
            }
        ],
        "excludedSymbols": [],
        "assumptions": [],
        "riskNotes": [],
    }

    plan = PackagingPlan.validate(
        raw,
        known_symbols=ir.known_symbols,
        symbol_required_parameters={
            symbol.qualifiedName: symbol.requiredParameters for symbol in ir.symbols
        },
    )

    assert plan.tool_names == ["parse_molecule"]


def test_plan_rejects_excluding_independent_user_capability(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"] = [raw["services"][0]["tools"][0]]
    raw["excludedSymbols"].append(
        {"symbol": "core.evaluate", "reason": "Non-core function that may be supported later."}
    )

    try:
        PackagingPlan.validate(
            raw,
            known_symbols=ir.known_symbols,
            candidate_symbols={"core.predict", "core.evaluate"},
            symbol_calls={symbol.qualifiedName: symbol.calls for symbol in ir.symbols},
        )
    except PlanValidationError as exc:
        assert "独立预测/计算能力" in str(exc)
        assert "core.evaluate" in str(exc)
    else:
        raise AssertionError("independent application capability should not be silently excluded")


def test_plan_requires_array_output_for_generator_source(tmp_path):
    project = _sample_project(tmp_path)
    core = project / "core.py"
    core.write_text(
        core.read_text(encoding="utf-8")
        + "\ndef predict_many(values: list[float]):\n"
        + "    for value in values:\n"
        + "        yield predict(value)\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    generator = next(symbol for symbol in ir.symbols if symbol.qualifiedName == "core.predict_many")
    assert generator.isGenerator
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][1]["sourceSymbols"] = ["core.predict_many"]

    try:
        PackagingPlan.validate(
            raw,
            known_symbols=ir.known_symbols,
            symbol_is_generator={symbol.qualifiedName: symbol.isGenerator for symbol in ir.symbols},
        )
    except PlanValidationError as exc:
        assert "使用 yield" in str(exc)
    else:
        raise AssertionError("generator source must expose an array output contract")


async def test_save_plan_tool_persists_only_valid_plan(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    tool = SavePackagingPlan(store)

    raw = _plan(ir).to_dict()
    raw["services"] = json.dumps(raw["services"], ensure_ascii=False)
    result = await tool.execute(**raw)

    assert not result.error
    assert store.plan is not None
    assert json.loads(store.path.read_text(encoding="utf-8"))["decision"] == "package"


async def test_save_plan_tool_normalizes_python_like_model_arguments(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    tool = SavePackagingPlan(store)
    raw = _plan(ir).to_dict()
    raw["services"] = repr(raw["services"]).replace("True", "true").replace("False", "false")
    raw["assumptions"] = "\n1. First assumption\n2. Second assumption\n"

    result = await tool.execute(**raw)

    assert not result.error
    assert store.plan is not None
    assert store.plan.data["assumptions"] == ["First assumption", "Second assumption"]


async def test_save_plan_json_fallback_uses_same_validation(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    tool = SavePackagingPlanJson(store)

    result = await tool.execute(content=json.dumps(_plan(ir).to_dict(), ensure_ascii=False))

    assert not result.error
    assert store.plan is not None
    assert store.plan.tool_names == ["predict_risk", "evaluate_risk"]


async def test_save_plan_json_canonicalizes_only_nonsemantic_fields(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    tool = SavePackagingPlanJson(store)
    raw = _plan(ir).to_dict()
    raw["services"][0].pop("id")
    for item in raw["services"][0]["tools"]:
        item.pop("evidence")
        item["smokeTest"]["evidence"] = item["smokeTest"]["evidence"][0]
    expected_strategy = raw["services"][0]["tools"][0].pop("adapterStrategy")
    raw["services"][0]["tools"][0]["adaptationStrategy"] = expected_strategy

    result = await tool.execute(content=json.dumps(raw, ensure_ascii=False))

    assert not result.error
    assert store.plan is not None
    assert store.plan.data["services"][0]["id"] == "risk_scoring"
    assert store.plan.tools[0]["evidence"] == ["core.predict"]
    assert store.plan.tools[0]["adapterStrategy"] == expected_strategy
    assert "adaptationStrategy" not in store.plan.tools[0]
    assert isinstance(store.plan.tools[0]["smokeTest"]["evidence"], list)


async def test_save_plan_json_does_not_hide_missing_smoke_contract(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    tool = SavePackagingPlanJson(store)
    raw = _plan(ir).to_dict()
    raw.pop("schemaVersion")
    raw["decision"] = "packaged"
    raw.pop("analysisSummary")
    raw["services"][0].pop("rationale")
    for item in raw["services"][0]["tools"]:
        item.pop("adapterStrategy")
        item.pop("dependsOn")
        item.pop("smokeTest")

    result = await tool.execute(content=json.dumps(raw, ensure_ascii=False))

    assert result.error
    assert "smokeTest" in result.error
    assert store.plan is None


async def test_save_plan_json_recovers_service_scoped_source_symbols(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    tool = SavePackagingPlanJson(store)
    raw = _plan(ir).to_dict()
    for service in raw["services"]:
        service["sourceSymbols"] = ["core.predict"]
        for item in service["tools"]:
            item.pop("sourceSymbols")
            item["evidence"] = [""]

    result = await tool.execute(content=json.dumps(raw, ensure_ascii=False))

    assert not result.error
    assert store.plan is not None
    assert all(item["sourceSymbols"] == ["core.predict"] for item in store.plan.tools)
    assert all(item["evidence"] == ["core.predict"] for item in store.plan.tools)


def test_extract_planning_json_recovers_plain_or_fenced_model_content() -> None:
    expected = {"decision": "accept", "services": []}

    assert json.loads(_extract_planning_json(json.dumps(expected)) or "null") == expected
    fenced = "Here is the plan:\n```json\n" + json.dumps(expected) + "\n```"
    assert json.loads(_extract_planning_json(fenced) or "null") == expected
    assert _extract_planning_json("I still need to inspect the repository") is None


async def test_planner_can_repair_multiple_quality_gate_failures(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )

    class FakePlanner:
        max_steps = 1

        def __init__(self, attempt):
            self.attempt = attempt
            self.calls = 0

        async def run(self, prompt):
            self.calls += 1
            if self.attempt == 3:
                await SavePackagingPlanJson(store).execute(
                    content=_plan(ir).to_json(indent=None)
                )
            yield AgentEvent(
                type="done",
                step=1,
                data={"result": "attempt complete"},
            )

    planners = [FakePlanner(0)]

    def fresh_planner():
        planner = FakePlanner(len(planners))
        planners.append(planner)
        return planner

    events = [
        event
        async for event in _run_planner(
            planners[0],
            store,
            ir,
            "package it",
            fresh_agent_factory=fresh_planner,
        )
    ]

    assert len(planners) == 4
    assert [planner.calls for planner in planners] == [1, 1, 1, 1]
    assert store.plan is not None
    assert len([event for event in events if event.type == "think"]) == 3


async def test_planner_retry_receives_previous_candidate_artifact(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )
    prompts: list[str] = []

    class FakePlanner:
        max_steps = 1

        def __init__(self, attempt: int):
            self.attempt = attempt

        async def run(self, prompt):
            prompts.append(prompt)
            if self.attempt == 0:
                rejected = _plan(ir).to_dict()
                rejected["analysisSummary"] = "previous candidate marker"
                rejected["services"] = []
                await SavePackagingPlanJson(store).execute(
                    content=json.dumps(rejected, ensure_ascii=False)
                )
            else:
                assert "上一版完整候选工件" in prompt
                assert "previous candidate marker" in prompt
                assert "services" in prompt
                await SavePackagingPlanJson(store).execute(
                    content=_plan(ir).to_json(indent=None)
                )
            yield AgentEvent(type="done", step=1, data={"result": "complete"})

    attempts = [FakePlanner(0)]

    def fresh_planner():
        planner = FakePlanner(len(attempts))
        attempts.append(planner)
        return planner

    events = [
        event
        async for event in _run_planner(
            attempts[0],
            store,
            ir,
            "package it",
            fresh_agent_factory=fresh_planner,
        )
    ]

    assert len(attempts) == 2
    assert len(prompts) == 2
    assert store.plan is not None
    assert store.last_candidate == store.plan.to_dict()
    assert any(event.type == "think" for event in events)


async def test_planner_allows_late_staged_refinement_to_converge(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )

    class FakePlanner:
        max_steps = 1

        def __init__(self, attempt: int):
            self.attempt = attempt

        async def run(self, prompt):
            rejected = _plan(ir).to_dict()
            rejected["services"] = []
            rejected["analysisSummary"] = f"staged candidate {self.attempt}"
            if self.attempt == 8:
                await SavePackagingPlanJson(store).execute(
                    content=_plan(ir).to_json(indent=None)
                )
            else:
                await SavePackagingPlanJson(store).execute(
                    content=json.dumps(rejected, ensure_ascii=False)
                )
            yield AgentEvent(type="done", step=1, data={"result": "complete"})

    attempts = [FakePlanner(0)]

    def fresh_planner():
        planner = FakePlanner(len(attempts))
        attempts.append(planner)
        return planner

    _ = [
        event
        async for event in _run_planner(
            attempts[0],
            store,
            ir,
            "package it",
            fresh_agent_factory=fresh_planner,
        )
    ]

    assert len(attempts) == 9
    assert store.plan is not None


async def test_save_plan_json_recovers_service_scoped_excluded_symbols(tmp_path):
    """Regression for the GNN plan that nested the repository audit in a service."""
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(
        tmp_path / "plan.json",
        ir.known_symbols,
        candidate_symbols={"core.predict", "core.evaluate"},
    )
    tool = SavePackagingPlanJson(store)
    raw = _plan(ir).to_dict()
    expected = raw.pop("excludedSymbols")
    raw["services"][0]["excludedSymbols"] = expected

    result = await tool.execute(content=json.dumps(raw, ensure_ascii=False))

    assert not result.error
    assert store.plan is not None
    assert store.plan.data["excludedSymbols"] == expected
    assert "excludedSymbols" not in store.plan.data["services"][0]


async def test_save_plan_enforces_reference_free_interface_quality_when_enabled(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(
        tmp_path / "plan.json",
        ir.known_symbols,
        enforce_interface_quality=True,
    )

    result = await SavePackagingPlanJson(store).execute(
        content=_plan(ir).to_json(indent=None)
    )

    assert result.error
    assert "接口质量门禁失败" in result.error
    assert store.plan is None
    assert store.interface_quality is not None
    assert not store.interface_quality.passed


async def test_save_plan_requires_independent_smoke_evidence_for_adapted_templates(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(
        tmp_path / "plan.json",
        ir.known_symbols,
        known_files={file.path for file in ir.files} | {"main.py"},
        require_independent_smoke_evidence=True,
    )
    raw = _plan(ir).to_dict()
    for tool in raw["services"][0]["tools"]:
        tool["smokeTest"]["evidence"] = ["main.py: generated template example"]

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert result.error
    assert "smoke 证据门禁失败" in result.error
    assert store.plan is None
    assert store.last_errors is not None
    assert all("只引用了生成的 main.py" in error for error in store.last_errors)


async def test_save_plan_requires_enabled_smoke_for_every_adapted_template_tool(
    tmp_path,
):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(
        tmp_path / "plan.json",
        ir.known_symbols,
        known_files={file.path for file in ir.files},
        require_independent_smoke_evidence=True,
    )
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["smokeTest"]["enabled"] = False
    raw["services"][0]["tools"][0]["smokeTest"]["rationale"] = (
        "No fixture was selected."
    )

    result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert result.error
    assert "smoke 证据门禁失败" in result.error
    assert store.plan is None
    assert store.last_errors is not None
    assert any(
        "predict_risk.smokeTest.enabled=false" in error
        for error in store.last_errors
    )


async def test_plan_store_keeps_most_advanced_rejected_candidate(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    store = PlanStore(
        tmp_path / "plan.json",
        ir.known_symbols,
        known_files={file.path for file in ir.files} | {"main.py"},
        require_independent_smoke_evidence=True,
        smoke_evidence_root=project,
    )
    advanced = _plan(ir).to_dict()
    advanced["analysisSummary"] = "advanced smoke checkpoint"
    for tool in advanced["services"][0]["tools"]:
        tool["smokeTest"]["evidence"] = ["main.py: generated example"]
    regressed = _plan(ir).to_dict()
    regressed["analysisSummary"] = "later structural regression"
    regressed["services"] = []

    advanced_result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(advanced, ensure_ascii=False)
    )
    regressed_result = await SavePackagingPlanJson(store).execute(
        content=json.dumps(regressed, ensure_ascii=False)
    )

    assert advanced_result.error and "smoke 证据门禁失败" in advanced_result.error
    assert regressed_result.error and "规划校验失败" in regressed_result.error
    assert store.last_candidate is not None
    assert store.last_candidate["analysisSummary"] == "later structural regression"
    assert store.best_candidate is not None
    assert store.best_candidate["analysisSummary"] == "advanced smoke checkpoint"
    assert store.best_score is not None and store.best_score[0] == 3
    assert store.best_errors is not None
    assert all("只引用了生成的 main.py" in error for error in store.best_errors)


async def test_save_plan_requires_free_text_smoke_values_from_cited_fixture(tmp_path):
    project = _sample_project(tmp_path)
    examples = project / "examples"
    examples.mkdir()
    (examples / "fixture.py").write_text(
        'SCENARIO = "documented risk fixture"\n',
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    predict_tool = raw["services"][0]["tools"][0]
    predict_tool["inputSchema"]["properties"]["scenario"] = {"type": "string"}
    predict_tool["smokeTest"]["input"]["scenario"] = "invented placeholder"
    predict_tool["smokeTest"]["evidence"] = ["README.md:1"]

    rejected_store = PlanStore(
        tmp_path / "rejected-plan.json",
        ir.known_symbols,
        known_files={file.path for file in ir.files},
        require_independent_smoke_evidence=True,
        smoke_evidence_root=project,
    )
    rejected = await SavePackagingPlanJson(rejected_store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert rejected.error
    assert "未在所引测试/doctest/示例中出现" in rejected.error
    assert "invented placeholder" in rejected.error
    assert "documented risk fixture" in rejected.error
    assert "examples/fixture.py:1" in rejected.error
    assert "同步更新 evidence" in rejected.error
    assert rejected_store.best_score is not None
    assert rejected_store.best_score[0] == 3

    predict_tool["smokeTest"]["input"]["scenario"] = "documented risk fixture"
    provenance_result = await SavePackagingPlanJson(rejected_store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )
    assert provenance_result.error
    assert "[smoke_evidence_reference]" in provenance_result.error
    assert "必须保持 input 不变" in provenance_result.error
    assert "'documented risk fixture' -> [" in provenance_result.error
    assert "evidence: examples/fixture.py:1" in provenance_result.error
    assert rejected_store.best_score is not None
    assert rejected_store.best_score[0] == 4
    assert rejected_store.best_candidate is not None
    assert (
        rejected_store.best_candidate["services"][0]["tools"][0]["smokeTest"][
            "input"
        ]["scenario"]
        == "documented risk fixture"
    )

    predict_tool["smokeTest"]["evidence"] = ["examples/fixture.py:1"]
    accepted_store = PlanStore(
        tmp_path / "accepted-plan.json",
        ir.known_symbols,
        known_files={file.path for file in ir.files},
        require_independent_smoke_evidence=True,
        smoke_evidence_root=project,
    )
    accepted = await SavePackagingPlanJson(accepted_store).execute(
        content=json.dumps(raw, ensure_ascii=False)
    )

    assert not accepted.error
    assert accepted_store.plan is not None


def test_smoke_fixture_suggestions_preserve_reaction_syntax_family():
    corpus = '''
UNRELATED_ASSIGNMENT = "not a reaction"

def test_reactions():
    assert Reaction.from_string("H2O -> H+ + OH-; 1e-4")
    assert Equilibrium.from_string("H2O = H+ + OH-; 1e-14")

def documented():
    """
    >>> line = '2 H2O -> 2 H2 + O2 ; 3e-4'
    """
'''

    equilibrium = _smoke_string_candidates(corpus, "A <-> B; K=3")
    kinetics = _smoke_string_candidates(corpus, "A -> B; k=0.2")

    assert equilibrium[0] == "H2O = H+ + OH-; 1e-14"
    assert all("->" not in candidate for candidate in equilibrium)
    assert all(";" in candidate for candidate in equilibrium)
    assert "H2O -> H+ + OH-; 1e-4" in kinetics
    assert "2 H2O -> 2 H2 + O2 ; 3e-4" in kinetics


async def test_save_plan_json_discards_only_unknown_excluded_symbols(tmp_path):
    """Provider hallucinations in audit-only exclusions must not block a valid plan."""
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(
        tmp_path / "plan.json",
        ir.known_symbols,
        candidate_symbols={"core.predict", "core.evaluate"},
    )
    tool = SavePackagingPlanJson(store)
    raw = _plan(ir).to_dict()
    raw["excludedSymbols"].append(
        {"symbol": "core.DoesNotExist", "reason": "Hallucinated provider audit entry."}
    )

    result = await tool.execute(content=json.dumps(raw, ensure_ascii=False))

    assert not result.error
    assert store.plan is not None
    assert store.plan.data["excludedSymbols"] == [
        {"symbol": "core._normalize", "reason": "internal helper"}
    ]


async def test_save_plan_reports_malformed_service_scoped_exclusions(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    store = PlanStore(tmp_path / "plan.json", ir.known_symbols)
    tool = SavePackagingPlanJson(store)
    raw = _plan(ir).to_dict()
    raw["services"][0]["excludedSymbols"] = "not-an-array"

    result = await tool.execute(content=json.dumps(raw, ensure_ascii=False))

    assert result.error
    assert "services[0].excludedSymbols 字段层级错误" in result.error
    assert "规划顶层 excludedSymbols" in result.error


def test_plan_rejects_operational_metadata_as_algorithm_tool(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["name"] = "get_model_info"

    try:
        PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    except PlanValidationError as exc:
        assert "不是用户算法能力" in str(exc)
    else:
        raise AssertionError("operational metadata must not be exposed as an algorithm tool")


def test_scaffold_and_verifier_accept_exact_multi_tool_contract(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")

    report = ArtifactVerifier(artifact, plan).verify()

    assert report.passed, report.to_json()
    assert report.checks["registeredTools"] == ["evaluate_risk", "predict_risk"]
    dockerfile = (artifact / "Dockerfile").read_text(encoding="utf-8")
    assert '"FROM' not in dockerfile
    assert "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
    assert '--index-url "${PIP_INDEX_URL}" --timeout 120 --retries 5' in dockerfile
    assert "PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu" in dockerfile
    assert "requirements-cpu.txt" in dockerfile
    assert "system-packages.txt" in dockerfile
    assert "sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d'" in dockerfile
    assert "USER 10001:10001" in dockerfile
    loader = (artifact / "algorithm_loader.py").read_text(encoding="utf-8")
    assert 'ALGORITHM_DIR / "src"' in loader
    assert "sys.path.append" in loader
    assert "sys.path.insert" not in loader


def test_verifier_explains_hidden_variadic_adapter_parameters(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    adapters = _valid_adapters().replace(
        "def predict_risk(value: float)",
        "def predict_risk(value: float, **kwargs)",
    )
    (artifact / "adapters.py").write_text(adapters, encoding="utf-8")

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any(
        "predict_risk" in error
        and "forbiddenVariadic=['**kwargs']" in error
        and "不得使用 *args/**kwargs" in error
        for error in report.errors
    )


def test_scaffold_preserves_agent_facing_schema_descriptions_and_constraints(
    tmp_path,
    monkeypatch,
):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    predict = raw["services"][0]["tools"][0]
    predict["description"] = (
        "Predict a normalized risk score for one observation when an agent needs "
        "a bounded value for a downstream decision or comparison."
    )
    predict["inputSchema"]["properties"]["value"] = {
        "type": "number",
        "description": "Observation value to normalize before risk scoring.",
        "minimum": 0,
        "maximum": 1,
        "examples": [0.5],
    }
    predict["outputSchema"]["properties"]["score"]["description"] = (
        "Normalized risk score in the inclusive range from zero to one."
    )
    predict["outputSchema"]["properties"]["error"] = {
        "type": "string",
        "description": "Failure detail returned only when risk scoring cannot complete.",
    }
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")

    monkeypatch.syspath_prepend(str(artifact))
    sys.modules.pop("adapters", None)
    spec = importlib.util.spec_from_file_location(
        "generated_schema_contract_server",
        artifact / "server.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = {tool.name: tool for tool in module.mcp._tool_manager.list_tools()}
    generated = tools["predict_risk"]

    value_schema = generated.parameters["properties"]["value"]
    assert value_schema["description"] == "Observation value to normalize before risk scoring."
    assert value_schema["minimum"] == 0
    assert value_schema["maximum"] == 1
    assert value_schema["examples"] == [0.5]
    assert generated.output_schema["properties"]["score"]["description"].startswith(
        "Normalized risk score"
    )
    assert "error" not in generated.output_schema["required"]
    _, structured = asyncio.run(
        generated.run({"value": 0.5}, convert_result=True)
    )
    assert structured == {"score": 0.5}
    assert "Args:" in generated.description
    assert "Returns:" in generated.description
    assert "value: Observation value" in generated.description
    assert "score (number)" in generated.description


def test_scaffold_publishes_exact_non_nullable_optional_schema(tmp_path, monkeypatch):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    predict = raw["services"][0]["tools"][0]
    predict["inputSchema"]["properties"]["threshold"] = {
        "type": "number",
        "description": "Optional decision threshold; omission delegates to the algorithm default.",
        "minimum": 0,
        "maximum": 1,
    }
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "adapters.py").write_text(
        "def predict_risk(value, threshold=None):\n"
        "    return {'score': value}\n\n"
        "def evaluate_risk(values):\n"
        "    return {'mean': sum(values) / len(values)}\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(artifact))
    sys.modules.pop("adapters", None)
    spec = importlib.util.spec_from_file_location(
        "generated_exact_schema_server",
        artifact / "server.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = module.mcp._tool_manager.get_tool("predict_risk")
    assert generated is not None

    assert generated.parameters == predict["inputSchema"]
    assert generated.output_schema == predict["outputSchema"]
    assert generated.parameters["properties"]["threshold"]["type"] == "number"
    assert "anyOf" not in generated.parameters["properties"]["threshold"]
    _, structured = asyncio.run(
        generated.run({"value": 0.5}, convert_result=True)
    )
    assert structured == {"score": 0.5}
    try:
        asyncio.run(
            generated.run(
                {"value": 0.5, "threshold": None},
                convert_result=True,
            )
        )
    except Exception as exc:
        assert "threshold" in str(exc)
    else:
        raise AssertionError("explicit null must be rejected by a non-null optional schema")


def test_scaffold_aliases_pydantic_reserved_output_fields(tmp_path, monkeypatch):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    predict = raw["services"][0]["tools"][0]
    predict["outputSchema"] = {
        "type": "object",
        "properties": {
            "score": {
                "type": "number",
                "description": "Normalized risk score.",
            },
            "model_config": {
                "type": "object",
                "description": "Configuration used by the model.",
                "additionalProperties": True,
            },
        },
        "required": ["score", "model_config"],
    }
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "adapters.py").write_text(
        "def predict_risk(value):\n"
        "    return {'score': value, 'model_config': {'threshold': 0.5}}\n\n"
        "def evaluate_risk(values):\n"
        "    return {'mean': sum(values) / len(values)}\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(artifact))
    sys.modules.pop("adapters", None)
    spec = importlib.util.spec_from_file_location(
        "generated_reserved_output_server",
        artifact / "server.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = module.mcp._tool_manager.get_tool("predict_risk")
    assert generated is not None

    _, structured = asyncio.run(
        generated.run({"value": 0.5}, convert_result=True)
    )

    assert structured == {
        "score": 0.5,
        "model_config": {"threshold": 0.5},
    }
    assert generated.output_schema == predict["outputSchema"]


def test_verifier_rejects_adapter_sys_path_mutation(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(
        "import sys\n"
        "from algorithm_loader import ALGORITHM_DIR\n"
        "sys.path.insert(0, str(ALGORITHM_DIR))\n"
        + _valid_adapters(),
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("不允许修改 sys.path" in error for error in report.errors)


def test_verifier_rejects_compatibility_shim_after_source_import(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(
        "from algorithm_loader import ALGORITHM_DIR\n"
        "import scipy.optimize as _optimize\n"
        "from core import predict as _predict, evaluate as _evaluate\n"
        "if not hasattr(_optimize, 'legacy_solver'):\n"
        "    _optimize.legacy_solver = _optimize.root\n\n"
        "def predict_risk(value: float) -> dict[str, float]:\n"
        "    return _predict(value)\n\n"
        "def evaluate_risk(values: list[float]) -> dict[str, float]:\n"
        "    return _evaluate(values)\n",
        encoding="utf-8",
    )
    requirements = (artifact / "requirements.txt").read_text(encoding="utf-8")
    (artifact / "requirements.txt").write_text(
        requirements + "\nscipy>=1.11\n",
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("兼容映射必须在源码入口模块导入前生效" in error for error in report.errors)


def test_verifier_rejects_unbounded_protocol_dependencies(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")
    (artifact / "requirements.txt").write_text(
        "numpy>=1.26\nmcp\nstarlette\nuvicorn\n",
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("必须保留平台已验证的 MCP 协议依赖范围" in error for error in report.errors)


def test_verifier_rejects_removing_template_runtime_dependency(tmp_path):
    project = _sample_project(tmp_path)
    (project / "requirements.txt").write_text(
        "numpy>=1.26\nquantities>=0.14\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")
    (artifact / "requirements.txt").write_text(
        "numpy>=1.26\n"
        "mcp>=1.28.0,<2\n"
        "starlette>=0.37.0,<2\n"
        "uvicorn[standard]>=0.30.0,<1\n",
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert report.checks["templateDeclaredDependencies"] == ["numpy", "quantities"]
    assert any(
        "删除了提交模板声明的运行依赖: quantities" in error
        for error in report.errors
    )


def test_verifier_recognizes_safe_explicit_source_module_loading(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(
        '''from algorithm_loader import ALGORITHM_DIR
import importlib.util

_spec = importlib.util.spec_from_file_location("_algorithm_core", ALGORITHM_DIR / "core.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("cannot load reviewed source")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_predict_source = _module.predict
_evaluate_source = _module.evaluate

def predict_risk(value: float) -> dict[str, float]:
    return _predict_source(value)

def evaluate_risk(values: list[float]) -> dict[str, float]:
    return _evaluate_source(values)
''',
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert report.passed, report.errors
    assert not any(
        "未调用规划中的任何源码能力" in error for error in report.errors
    )


def test_verifier_rejects_unrooted_explicit_module_loading(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(
        '''from algorithm_loader import ALGORITHM_DIR
import importlib.util

_spec = importlib.util.spec_from_file_location("_algorithm_core", "/tmp/core.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("cannot load source")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_predict_source = _module.predict
_evaluate_source = _module.evaluate

def predict_risk(value: float) -> dict[str, float]:
    return _predict_source(value)

def evaluate_risk(values: list[float]) -> dict[str, float]:
    return _evaluate_source(values)
''',
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert sum(
        "未调用规划中的任何源码能力" in error for error in report.errors
    ) == 2


def test_verifier_rejects_reimplementing_source_with_another_library(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(
        "import statistics\n\n"
        "def predict_risk(value: float) -> dict[str, float]:\n"
        "    return {'score': float(value)}\n\n"
        "def evaluate_risk(values: list[float]) -> dict[str, float]:\n"
        "    return {'mean_score': statistics.mean(values)}\n",
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert sum("未调用规划中的任何源码能力" in error for error in report.errors) == 2
    assert any("禁止用另一个库重写算法" in error for error in report.errors)


def test_verifier_accepts_reviewed_source_classmethod_invocation(tmp_path):
    project = _sample_project(tmp_path)
    core = project / "core.py"
    core.write_text(
        core.read_text(encoding="utf-8")
        + "\nclass RiskModel:\n"
        + "    @classmethod\n"
        + "    def from_value(cls, value: float) -> dict[str, float]:\n"
        + "        return {'score': float(value)}\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["sourceSymbols"] = ["core.RiskModel"]
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    adapters = _valid_adapters().replace(
        "from algorithm.core import predict as algorithm_predict",
        "from algorithm.core import RiskModel as algorithm_predict",
    ).replace(
        "return algorithm_predict(float(value))",
        "return algorithm_predict.from_value(float(value))",
    )
    (artifact / "adapters.py").write_text(adapters, encoding="utf-8")

    report = ArtifactVerifier(artifact, plan).verify()

    assert report.passed, report.to_json()


def test_scaffold_splits_cpu_wheels_and_drops_unsafe_source_requirements(tmp_path):
    project = _sample_project(tmp_path)
    (project / "requirements.txt").write_text(
        "numpy>=1.26\n"
        "torch==2.4.1\n"
        "torchvision>=0.19\n"
        "--extra-index-url https://example.test/simple\n"
        "demo @ https://example.test/demo.whl\n"
        "-e ../local-package\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)

    artifact = prepare_artifact(project, tmp_path / "artifact", _plan(ir))

    general = (artifact / "requirements.txt").read_text(encoding="utf-8")
    cpu = (artifact / "requirements-cpu.txt").read_text(encoding="utf-8")
    assert "numpy>=1.26" in general
    assert "torch" not in general
    assert "torch==2.4.1" in cpu
    assert "torchvision>=0.19" in cpu
    assert "example.test" not in general + cpu
    assert "../local-package" not in general + cpu


def test_scaffold_uses_reviewed_pure_python_source_instead_of_pypi_copy(tmp_path):
    project = _sample_project(tmp_path)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "risk-model"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    package = project / "risk_model"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'submitted-source'\n", encoding="utf-8")
    (project / "requirements.txt").write_text(
        "risk-model>=9\nnumpy>=1.26\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)

    artifact = prepare_artifact(project, tmp_path / "artifact", _plan(ir))

    requirements = (artifact / "requirements.txt").read_text(encoding="utf-8")
    assert "risk-model" not in requirements
    assert "numpy>=1.26" in requirements


def test_scaffold_preserves_pure_python_project_install_dependencies(tmp_path):
    project = _sample_project(tmp_path)
    (project / "setup.py").write_text(
        "import sys\n"
        "from setuptools import setup\n"
        "setup_kwargs = dict(\n"
        "    name='risk-model',\n"
        "    install_requires=[\n"
        "        'numpy>=1.20',\n"
        "        'quantities>=0.12.1',\n"
        "        'pyodesys>=0.14.5' if sys.version_info[0] >= 3 else 'pyodesys<0.12',\n"
        "        'unsafe @ https://example.test/unsafe.whl',\n"
        "    ],\n"
        ")\n"
        "setup(**setup_kwargs)\n",
        encoding="utf-8",
    )
    package = project / "risk_model"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'submitted-source'\n", encoding="utf-8")
    (project / "requirements.txt").write_text("risk-model>=9\nnumpy>=1.26\n", encoding="utf-8")
    ir = RepositoryAnalyzer().analyze(project)

    artifact = prepare_artifact(project, tmp_path / "artifact", _plan(ir))

    requirements = (artifact / "requirements.txt").read_text(encoding="utf-8")
    assert "risk-model" not in requirements
    assert "numpy>=1.26" in requirements
    assert "numpy>=1.20" not in requirements
    assert "quantities>=0.12.1" in requirements
    assert "pyodesys>=0.14.5" in requirements
    assert "pyodesys<0.12" not in requirements
    assert "example.test" not in requirements


def test_verifier_rejects_wheel_shadowing_of_reviewed_pure_python_source(tmp_path):
    project = _sample_project(tmp_path)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "risk-model"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    package = project / "risk_model"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'submitted-source'\n", encoding="utf-8")
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")
    requirements = artifact / "requirements.txt"
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "risk-model>=9\n",
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert report.checks["sourceOwnedDistributions"] == ["risk-model"]
    assert any(
        "site-packages 会覆盖已审核源码" in error and "risk-model" in error
        for error in report.errors
    )


def test_scaffold_reads_static_pyproject_and_setup_cfg_dependencies(tmp_path):
    project = _sample_project(tmp_path)
    (project / "pyproject.toml").write_text(
        '[project]\n'
        'name = "risk-model"\n'
        'version = "1.0.0"\n'
        'dependencies = ["pydantic>=2", "torch==2.4.1"]\n',
        encoding="utf-8",
    )
    (project / "setup.cfg").write_text(
        "[metadata]\n"
        "name = risk-model\n"
        "[options]\n"
        "install_requires =\n"
        "    scipy>=1.11\n"
        "    Pillow>=10\n",
        encoding="utf-8",
    )
    package = project / "risk_model"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'submitted-source'\n", encoding="utf-8")
    ir = RepositoryAnalyzer().analyze(project)

    artifact = prepare_artifact(project, tmp_path / "artifact", _plan(ir))

    general = (artifact / "requirements.txt").read_text(encoding="utf-8")
    cpu = (artifact / "requirements-cpu.txt").read_text(encoding="utf-8")
    assert "pydantic>=2" in general
    assert "scipy>=1.11" in general
    assert "Pillow>=10" in general
    assert "torch==2.4.1" in cpu


def test_scaffold_keeps_wheel_dependency_for_compiled_source_wrapper(tmp_path):
    project = _sample_project(tmp_path)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "risk-model"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    package = project / "risk_model"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'python-wrapper'\n", encoding="utf-8")
    (project / "setup.py").write_text(
        "from setuptools import Extension, setup\n"
        "setup(name='risk-model', ext_modules=[Extension('risk_model._core', ['core.c'])])\n",
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text("risk-model>=1\n", encoding="utf-8")
    ir = RepositoryAnalyzer().analyze(project)

    artifact = prepare_artifact(project, tmp_path / "artifact", _plan(ir))

    assert "risk-model>=1" in (artifact / "requirements.txt").read_text(encoding="utf-8")


def test_dependency_inspector_follows_local_import_chain_once(tmp_path):
    algorithm = tmp_path / "algorithm"
    package = algorithm / "src" / "localpkg"
    package.mkdir(parents=True)
    (algorithm / "main.py").write_text(
        "import numpy\nfrom localpkg.worker import run\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        "from .worker import run\n",
        encoding="utf-8",
    )
    (package / "worker.py").write_text(
        "from PIL import Image\n"
        "import lmdb\n"
        "import sympy as sm\n"
        "jit_backend = sm.external.import_module('jit_backend')\n"
        "if jit_backend:\n"
        "    from jit_backend import compile_func\n"
        "try:\n"
        "    import optional_accelerator\n"
        "except ImportError:\n"
        "    optional_accelerator = None\n"
        "def deferred_backend():\n"
        "    import runtime_selected_backend\n"
        "    return runtime_selected_backend\n"
        "def run():\n"
        "    return Image, lmdb\n",
        encoding="utf-8",
    )
    adapters = tmp_path / "adapters.py"
    adapters.write_text(
        "from algorithm_loader import ALGORITHM_DIR\nfrom main import run\n",
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("numpy>=1.26\nPillow>=10\nsympy>=1.12\n", encoding="utf-8")

    unresolved = unresolved_import_dependencies(
        algorithm,
        source_modules={"main"},
        adapter_path=adapters,
        requirement_paths=(requirements,),
    )

    assert unresolved == {
        "lmdb": {
            "distribution": "lmdb",
            "files": ["src/localpkg/worker.py"],
        }
    }


def test_verifier_blocks_incomplete_agent_output(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(
        _valid_server().replace("@mcp.tool()\ndef evaluate_risk", "def evaluate_risk"),
        encoding="utf-8",
    )
    (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("evaluate_risk" in error and "缺少" in error for error in report.errors)
    assert not (artifact / ".ioeb-ready").exists()


def test_runtime_zip_guardrail_accepts_safe_and_rejects_traversal():
    safe = io.BytesIO()
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("dataset/meta.yaml", "dataset_name: demo")
    decoded = decode_safe_zip(base64.b64encode(safe.getvalue()).decode("ascii"))
    assert zipfile.is_zipfile(decoded)

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../../escape.txt", "bad")
    try:
        decode_safe_zip(base64.b64encode(unsafe.getvalue()).decode("ascii"))
    except ValueError as exc:
        assert "unsafe ZIP member" in str(exc)
    else:
        raise AssertionError("path traversal ZIP should be rejected")


def test_uploaded_repository_zip_extraction_is_path_contained(tmp_path):
    safe_zip = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe_zip, "w") as archive:
        archive.writestr("repo/main.py", "print('safe')")
    extracted = extract_zip(safe_zip, tmp_path / "safe-out")
    assert (extracted / "repo/main.py").read_text(encoding="utf-8") == "print('safe')"

    unsafe_zip = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe_zip, "w") as archive:
        archive.writestr("../../escaped.py", "bad")
    try:
        extract_zip(unsafe_zip, tmp_path / "unsafe-out")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "不安全路径" in str(exc.detail)
    else:
        raise AssertionError("uploaded traversal ZIP should be rejected")
    assert not (tmp_path / "escaped.py").exists()


def test_verifier_rejects_import_shadowing_and_failure_as_success(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(
        '''from algorithm.core import predict as predict_risk
from algorithm.core import evaluate as evaluate_risk

def predict_risk(value: float) -> dict[str, float]:
    try:
        return {"score": predict_risk(value)}
    except Exception as exc:
        return {"score": -1.0}

def evaluate_risk(values: list[float]) -> dict[str, float]:
    return evaluate_risk(values)
''',
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("伪装成成功" in error for error in report.errors)
    assert any("递归调用" in error for error in report.errors)


def test_verifier_requires_guard_on_source_failure_sentinel(tmp_path):
    project = _sample_project(tmp_path)
    core = project / "core.py"
    core.write_text(
        core.read_text(encoding="utf-8")
        + '\ndef fragile(value: float) -> dict[str, float] | str:\n'
        + '    try:\n'
        + '        return {"score": float(value)}\n'
        + '    except Exception as exc:\n'
        + '        return f"error: {exc}"\n\n'
        + 'def run_fragile(value: float) -> dict[str, float] | str:\n'
        + '    return fragile(value)\n',
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    fragile = next(symbol for symbol in ir.symbols if symbol.qualifiedName == "core.fragile")
    assert fragile.failureReturns == ["error:"]
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["sourceSymbols"] = ["core.run_fragile"]
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    unguarded = _valid_adapters().replace(
        "from algorithm.core import predict as algorithm_predict",
        "from algorithm.core import run_fragile as algorithm_predict",
    )
    (artifact / "adapters.py").write_text(unguarded, encoding="utf-8")

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("返回值并 raise" in error for error in report.errors)

    guarded = unguarded.replace(
        "    return algorithm_predict(float(value))",
        '    result = algorithm_predict(float(value))\n'
        '    if isinstance(result, str) and result.lower().startswith("error"):\n'
        '        raise RuntimeError(result)\n'
        '    return result',
    )
    (artifact / "adapters.py").write_text(guarded, encoding="utf-8")

    fixed_report = ArtifactVerifier(artifact, plan).verify()

    assert fixed_report.passed, fixed_report.to_json()

    guarded_by_result_helper = unguarded.replace(
        "    return algorithm_predict(float(value))",
        "    result = algorithm_predict(float(value))\n"
        "    _raise_on_failure(result)\n"
        "    return result",
    )
    guarded_by_result_helper += (
        "\n\ndef _raise_on_failure(result: dict) -> None:\n"
        '    if result.get("success") is False:\n'
        '        raise RuntimeError(result.get("error", "algorithm failed"))\n'
    )
    (artifact / "adapters.py").write_text(
        guarded_by_result_helper, encoding="utf-8"
    )

    helper_report = ArtifactVerifier(artifact, plan).verify()

    assert helper_report.passed, helper_report.to_json()

    guarded_by_transitive_result_helper = unguarded.replace(
        "    return algorithm_predict(float(value))",
        "    result = algorithm_predict(float(value))\n"
        "    return _unwrap_result(result)",
    )
    guarded_by_transitive_result_helper += (
        "\n\ndef _unwrap_result(result: dict) -> dict:\n"
        "    _raise_on_failure(result)\n"
        "    return result\n"
        "\n\ndef _raise_on_failure(result: dict) -> None:\n"
        '    if result.get("success") is False:\n'
        '        raise RuntimeError(result.get("error", "algorithm failed"))\n'
    )
    (artifact / "adapters.py").write_text(
        guarded_by_transitive_result_helper,
        encoding="utf-8",
    )

    transitive_helper_report = ArtifactVerifier(artifact, plan).verify()

    assert transitive_helper_report.passed, transitive_helper_report.to_json()

    guarded_by_helper = unguarded.replace(
        "    return algorithm_predict(float(value))",
        "    return _checked_predict(float(value))",
    )
    guarded_by_helper += (
        "\n\ndef _checked_predict(value: float) -> dict[str, float]:\n"
        "    result = algorithm_predict(value)\n"
        "    if isinstance(result, str) and result.lower().startswith('error'):\n"
        "        raise RuntimeError(result)\n"
        "    return result\n"
    )
    (artifact / "adapters.py").write_text(guarded_by_helper, encoding="utf-8")

    helper_report = ArtifactVerifier(artifact, plan).verify()

    assert helper_report.passed, helper_report.to_json()


def test_verifier_requires_guard_on_structured_failure_result(tmp_path):
    project = _sample_project(tmp_path)
    core = project / "core.py"
    core.write_text(
        core.read_text(encoding="utf-8")
        + "\ndef structured(value: float) -> dict:\n"
        + "    if value < 0:\n"
        + '        return {"success": False, "error": "negative value"}\n'
        + '    return {"success": True, "score": value}\n',
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    structured = next(symbol for symbol in ir.symbols if symbol.qualifiedName == "core.structured")
    assert structured.failureReturns == ["structured failure: success=false"]
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["sourceSymbols"] = ["core.structured"]
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    unguarded = _valid_adapters().replace(
        "from algorithm.core import predict as algorithm_predict",
        "from algorithm.core import structured as algorithm_predict",
    )
    (artifact / "adapters.py").write_text(unguarded, encoding="utf-8")

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("返回值并 raise" in error for error in report.errors)

    guarded = unguarded.replace(
        "    return algorithm_predict(float(value))",
        "    result = algorithm_predict(float(value))\n"
        '    if result.get("success") is False:\n'
        '        raise RuntimeError(result.get("error", "algorithm failed"))\n'
        "    return result",
    )
    (artifact / "adapters.py").write_text(guarded, encoding="utf-8")

    fixed_report = ArtifactVerifier(artifact, plan).verify()

    assert fixed_report.passed, fixed_report.to_json()


def test_verifier_rejects_legacy_import_before_loader_and_wrong_asset_root(tmp_path):
    project = _sample_project(tmp_path)
    (project / "risk-model.bin").write_bytes(b"model")
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    (artifact / "adapters.py").write_text(
        '''from pathlib import Path
from core import evaluate as algorithm_evaluate
from core import predict as algorithm_predict
from algorithm_loader import ALGORITHM_DIR

MODEL_PATH = Path(__file__).resolve().parent / "risk-model.bin"

def predict_risk(value: float) -> dict[str, float]:
    return algorithm_predict(value)

def evaluate_risk(values: list[float]) -> dict[str, float]:
    return algorithm_evaluate(values)
''',
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("源码模块导入前" in error for error in report.errors)
    assert any("risk-model.bin" in error and "ALGORITHM_DIR" in error for error in report.errors)


def test_verifier_does_not_treat_extensionless_data_name_as_asset_path(tmp_path):
    project = _sample_project(tmp_path)
    (project / "data").write_bytes(b"opaque fixture")
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
    adapters = _valid_adapters().replace(
        '"""Predict one normalized risk score."""',
        '"""Predict one score after validating input data."""',
    )
    (artifact / "adapters.py").write_text(adapters, encoding="utf-8")

    report = ArtifactVerifier(artifact, plan).verify()

    assert report.passed, report.to_json()


def test_verifier_requires_direct_single_base64_zip_guard(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][0]["inputSchema"] = {
        "type": "object",
        "properties": {
            "zip_file_data": {
                "type": "string",
                "description": "Base64 encoded ZIP file",
            }
        },
        "required": ["zip_file_data"],
    }
    raw["services"][0]["tools"][0]["smokeTest"] = {
        "enabled": False,
        "rationale": "The unit test only exercises the generated guardrail contract.",
    }
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "adapters.py").write_text(
        '''import base64
from algorithm.core import evaluate as algorithm_evaluate
from algorithm.core import predict as algorithm_predict
from runtime_guardrails import decode_safe_zip

def predict_risk(zip_file_data: str) -> dict[str, float]:
    stream = decode_safe_zip(base64.b64decode(zip_file_data))
    return {"score": float(len(stream.read()))}

def evaluate_risk(values: list[float]) -> dict[str, float]:
    return algorithm_evaluate(values)
''',
        encoding="utf-8",
    )

    report = ArtifactVerifier(artifact, plan).verify()

    assert not report.passed
    assert any("直接传给" in error for error in report.errors)
    assert any("重复 Base64 解码" in error for error in report.errors)


def test_analysis_cache_is_content_addressed_and_immutable(tmp_path):
    ir = RepositoryAnalyzer().analyze(_sample_project(tmp_path))
    plan = _plan(ir)
    cache = AnalysisCache(max_entries=2, ttl_seconds=60)
    cache.put(ir.fingerprint, plan)

    loaded = cache.get(ir.fingerprint)
    assert loaded is not None
    loaded.data["services"][0]["tools"][0]["name"] = "mutated"
    assert cache.get(ir.fingerprint).tool_names[0] == "predict_risk"


async def test_packaging_workflow_repairs_then_marks_artifact_ready(tmp_path, monkeypatch):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = tmp_path / "artifact"

    class FakeBuilder:
        max_steps = 2

        def __init__(self, attempt):
            self.attempt = attempt
            self.calls = 0

        def cancel(self):
            pass

        async def run(self, prompt):
            self.calls += 1
            if self.attempt == 0:
                (artifact / "server.py").write_text("def broken(:\n", encoding="utf-8")
                (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")
            else:
                (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
            yield AgentEvent(type="done", step=1, data={"result": "attempt complete"})

    fake_builders = []

    def build_fresh_agent(*args, **kwargs):
        builder = FakeBuilder(len(fake_builders))
        fake_builders.append(builder)
        return builder

    monkeypatch.setattr(
        "micro_agent.packaging.workflow._build_builder_agent",
        build_fresh_agent,
    )
    workflow = AgenticPackagingWorkflow(
        project_dir=project,
        ir=ir,
        artifact_dir=artifact,
        plan=plan,
        max_repairs=2,
    )

    events = [event async for event in workflow.run("package it")]

    assert len(fake_builders) == 2
    assert [builder.calls for builder in fake_builders] == [1, 1]
    assert events[-1].type == "done"
    marker = json.loads((artifact / ".ioeb-ready").read_text(encoding="utf-8"))
    assert marker["toolCount"] == 2
    assert marker["repairAttempts"] == 1
    assert marker["staticRepairAttempts"] == 1
    assert marker["runtimeRepairAttempts"] == 0
    assert marker["validationMode"] == "static_only"
    assert marker["runtimeVerified"] is False
    assert marker["functionalVerified"] is False
    assert marker["readinessLevel"] == "static_contract"


async def test_dependency_writer_allows_only_safe_package_manifests(tmp_path):
    writer = WriteArtifactFile(tmp_path)

    valid_requirements = await writer.execute(
        path="requirements.txt",
        content="numpy>=1.26\nmcp>=1.28.0,<2\n",
    )
    assert not valid_requirements.error
    assert (tmp_path / "requirements.txt").is_file()

    valid_cpu_requirements = await writer.execute(
        path="requirements-cpu.txt",
        content="torch>=2.4\ntorchvision>=0.19\n",
    )
    assert not valid_cpu_requirements.error

    invalid_cpu_requirement = await writer.execute(
        path="requirements-cpu.txt",
        content="numpy>=1.26\n",
    )
    assert invalid_cpu_requirement.error
    assert "只允许 torch" in invalid_cpu_requirement.error

    vcs_requirement = await writer.execute(
        path="requirements.txt",
        content="demo @ https://example.test/demo.whl\n",
    )
    assert vcs_requirement.error
    assert "URL/VCS" in vcs_requirement.error

    algorithm = tmp_path / "algorithm"
    package = algorithm / "local_solver"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (algorithm / "pyproject.toml").write_text(
        '[project]\nname = "local-solver"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    shadowing_requirement = await writer.execute(
        path="requirements.txt",
        content="local-solver>=1\nnumpy>=1.26\n",
    )
    assert shadowing_requirement.error
    assert "覆盖已审核源码: local-solver" in shadowing_requirement.error

    valid_system = await writer.execute(
        path="system-packages.txt",
        content="libgomp1\nlibexpat1\n",
    )
    assert not valid_system.error

    command_injection = await writer.execute(
        path="system-packages.txt",
        content="libgomp1\n$(touch /tmp/escaped)\n",
    )
    assert command_injection.error
    assert "Debian 包名" in command_injection.error

    immutable = await writer.execute(path="Dockerfile", content="FROM busybox\n")
    assert immutable.error


async def test_dependency_writer_blocks_removing_template_runtime_contract(tmp_path):
    algorithm = tmp_path / "algorithm"
    algorithm.mkdir()
    (algorithm / "requirements.txt").write_text(
        "numpy>=1.26\nquantities>=0.14\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text(
        "numpy>=1.26\nquantities>=0.14\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements-cpu.txt").write_text("", encoding="utf-8")
    writer = WriteArtifactFile(tmp_path)

    result = await writer.execute(
        path="requirements.txt",
        content="numpy>=1.26\n",
    )

    assert result.error
    assert "不得删除提交模板声明的运行依赖: quantities" in result.error
    assert "quantities" in (tmp_path / "requirements.txt").read_text(encoding="utf-8")


async def test_artifact_patcher_requires_one_exact_safe_match(tmp_path):
    (tmp_path / "adapters.py").write_text(
        "def calculate(value):\n    return value\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("numpy>=1.26\n", encoding="utf-8")
    patcher = PatchArtifactFile(tmp_path)

    patched = await patcher.execute(
        path="adapters.py",
        old_text="    return value\n",
        new_text="    return float(value)\n",
    )
    assert not patched.error
    assert "return float(value)" in (tmp_path / "adapters.py").read_text(encoding="utf-8")

    missing = await patcher.execute(
        path="adapters.py",
        old_text="return missing",
        new_text="return fixed",
    )
    assert "出现 0 次" in missing.error

    repeated = await patcher.execute(
        path="adapters.py",
        old_text="value",
        new_text="item",
    )
    assert "出现 2 次" in repeated.error

    empty = await patcher.execute(path="adapters.py", old_text="", new_text="pass\n")
    assert "不能为空" in empty.error

    invalid_requirement = await patcher.execute(
        path="requirements.txt",
        old_text="numpy>=1.26",
        new_text="-i https://example.test/simple",
    )
    assert "禁止 pip 命令行选项" in invalid_requirement.error
    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8") == "numpy>=1.26\n"

    immutable = await patcher.execute(
        path="server.py",
        old_text="old",
        new_text="new",
    )
    assert "不允许" in immutable.error


async def test_artifact_writer_requires_patches_after_initial_generation(tmp_path):
    writer = WriteArtifactFile(tmp_path)
    first = await writer.execute(path="adapters.py", content="value = 1\n")
    assert not first.error

    writer.lock_nonempty_overwrites()
    overwrite = await writer.execute(path="adapters.py", content="value = 2\n")
    initialize_empty = await writer.execute(
        path="system-packages.txt",
        content="libgomp1\n",
    )

    assert overwrite.error
    assert "patch_artifact_file" in overwrite.error
    assert not initialize_empty.error
    assert (tmp_path / "adapters.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "system-packages.txt").read_text(encoding="utf-8") == "libgomp1\n"


def test_repair_prompt_embeds_bounded_mutable_snapshot(tmp_path):
    (tmp_path / "adapters.py").write_text("a" * 30, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (tmp_path / "requirements-cpu.txt").write_text("", encoding="utf-8")
    (tmp_path / "system-packages.txt").write_text("", encoding="utf-8")
    snapshot = _repair_artifact_snapshot(
        tmp_path,
        max_file_chars=20,
        max_total_chars=40,
    )
    report = VerificationReport(
        passed=False,
        checks={},
        errors=["[runtime] failure"],
    )
    prompt = _repair_prompt(
        report,
        1,
        failure_phase="runtime",
        phase_attempt=1,
        artifact_snapshot=snapshot,
        implementation_context={"packagingPlan": {"decision": "package"}},
    )

    assert snapshot["adapters.py"] == "aaaaa\n...(truncated)"
    assert '"requirements.txt": "numpy\\n"' in prompt
    assert sum(len(value) for value in snapshot.values()) <= 40
    assert "第一项产物修改必须是 patch_artifact_file" in prompt
    assert "初始化快照中明确为空的目标文件" in prompt
    assert "可选导入或纯 Python fallback" in prompt
    assert "不得先调用 inspect_repository 或 read_project_file" in prompt
    assert '"decision": "package"' in prompt
    assert "[runtime] failure" in prompt

    smoke_prompt = _repair_prompt(
        VerificationReport(
            passed=False,
            checks={},
            errors=["[smoke_test] invalid fixture"],
        ),
        2,
        failure_phase="runtime",
        phase_attempt=2,
        artifact_snapshot=snapshot,
        implementation_context={"packagingPlan": {"decision": "package"}},
        allow_smoke_revision=True,
    )
    assert "revise_smoke_tests" in smoke_prompt
    assert "toolName/input/evidence 局部修订" in smoke_prompt

    forced_prompt = _repair_prompt(
        VerificationReport(
            passed=False,
            checks={"smokeTestFailures": {"predict_risk": "same failure"}},
            errors=["[smoke_test] same failure"],
        ),
        3,
        failure_phase="runtime",
        phase_attempt=3,
        artifact_snapshot=snapshot,
        implementation_context={"packagingPlan": {"decision": "package"}},
        allow_smoke_revision=True,
        force_smoke_revision=True,
    )
    assert "必须调用 revise_smoke_tests" in forced_prompt
    assert "不允许修改任何产物或依赖" in forced_prompt
    assert "第一项产物修改必须是 patch_artifact_file" not in forced_prompt


async def test_container_runtime_verifier_builds_and_discovers_tools(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        if command[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(command, 0, stdout="built", stderr="")
        if command[:2] == ["docker", "run"]:
            payload = {
                "registeredTools": sorted(plan.tool_names),
                "smokeTestsExecuted": sorted(plan.tool_names),
                "smokeTestCount": 2,
            }
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=PROBE_MARKER + json.dumps(payload) + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    report = await ContainerRuntimeVerifier(
        artifact,
        plan,
        command_runner=runner,
    ).verify()

    assert report.passed, report.to_json()
    assert report.checks["runtimeBackend"] == "docker"
    assert report.checks["smokeTestCount"] == 2
    build_command = next(command for command, _ in commands if command[:2] == ["docker", "build"])
    assert not any(part.startswith("--progress") for part in build_command)
    run_command = next(command for command, _ in commands if command[:2] == ["docker", "run"])
    assert "--network" in run_command
    assert "none" in run_command
    assert "--read-only" in run_command
    assert ["docker", "image", "rm"] == next(
        command[:3] for command, _ in commands if command[:3] == ["docker", "image", "rm"]
    )


def test_runtime_probe_is_valid_python_and_checks_smoke_output_schema():
    source = _runtime_probe_source(17)

    compile(source, "<runtime-probe>", "exec")
    assert "assert_schema(" in source
    assert "imported_attribute_gaps()" in source
    assert '"runtimeApiCompatibilityFailures": api_gaps' in source
    assert '"runtimeApiCompatibilitySuggestions": api_suggestions' in source
    assert '"runtimeApiCompatibilityObjects": api_objects' in source
    assert '"isNone": value is None' in source
    assert "def compatibility_candidates(module, missing_name):" in source
    assert "smoke output schema mismatch" in source
    assert '"smokeTestFailures": smoke_failures' in source
    assert "def schema_variants(tool, base_input):" in source
    assert '"schemaVariantsExecuted": schema_variants_executed' in source
    assert 'schema.get("additionalProperties")' in source
    assert "timeout=17" in source


def test_runtime_probe_varies_optional_enum_array_boolean_and_nullable_fields():
    source = _runtime_probe_source(17)
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "schema_variants"
    )
    namespace: dict[str, object] = {}
    executable = ast.Module(
        body=[
            ast.Import(names=[ast.alias(name="copy")]),
            ast.Import(names=[ast.alias(name="json")]),
            function,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(executable)
    exec(compile(executable, "<schema-variants>", "exec"), namespace)
    schema_variants = namespace["schema_variants"]
    assert callable(schema_variants)

    tool = {
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "exact"]},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["score", "confidence"]},
                    "minItems": 1,
                },
                "normalize": {"type": "boolean"},
                "threshold": {"type": ["number", "null"]},
            },
        }
    }
    variants = dict(schema_variants(tool, {}))

    assert variants["mode='fast'"] == {"mode": "fast"}
    assert variants["mode='exact'"] == {"mode": "exact"}
    assert variants["metrics[]='score'"] == {"metrics": ["score"]}
    assert variants["metrics[]='confidence'"] == {"metrics": ["confidence"]}
    assert variants["normalize=False"] == {"normalize": False}
    assert variants["normalize=True"] == {"normalize": True}
    assert variants["threshold=None"] == {"threshold": None}


async def test_container_runtime_verifier_preserves_partial_smoke_results(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)

    def runner(command, **kwargs):
        if command[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(command, 0, stdout="built", stderr="")
        if command[:2] == ["docker", "run"]:
            payload = {
                "registeredTools": sorted(plan.tool_names),
                "smokeTestsExecuted": ["predict_risk"],
                "smokeTestCount": 1,
                "smokeTestFailures": {
                    "evaluate_risk": "ToolError: missing compatible descriptor",
                },
            }
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=PROBE_MARKER + json.dumps(payload) + "\n",
                stderr="RuntimeError: smoke test failures",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    report = await ContainerRuntimeVerifier(
        artifact,
        plan,
        command_runner=runner,
    ).verify()

    assert not report.passed
    assert report.checks["smokeTestCount"] == 1
    assert report.checks["smokeTestFailures"] == {
        "evaluate_risk": "ToolError: missing compatible descriptor"
    }
    assert report.checks["functionalVerified"] is False
    assert any("[smoke_test]" in error for error in report.errors)


async def test_container_runtime_verifier_classifies_dependency_failure(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="ERROR: No matching distribution found for impossible==9",
        )

    report = await ContainerRuntimeVerifier(
        artifact,
        plan,
        command_runner=runner,
    ).verify()

    assert not report.passed
    assert report.checks["buildExitCode"] == 1
    assert any("[dependency_resolution]" in error for error in report.errors)


async def test_container_runtime_verifier_can_require_full_smoke_coverage(tmp_path):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    raw = _plan(ir).to_dict()
    raw["services"][0]["tools"][1]["smokeTest"] = {
        "enabled": False,
        "rationale": "No fixture available.",
    }
    plan = PackagingPlan.validate(raw, known_symbols=ir.known_symbols)
    artifact = prepare_artifact(project, tmp_path / "artifact", plan)
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    report = await ContainerRuntimeVerifier(
        artifact,
        plan,
        require_full_smoke_coverage=True,
        command_runner=runner,
    ).verify()

    assert not report.passed
    assert any("[smoke_coverage]" in error for error in report.errors)
    assert report.checks["smokeCoverage"] == 0.5
    assert commands == []


async def test_packaging_workflow_repairs_runtime_failure_before_ready(tmp_path, monkeypatch):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = tmp_path / "artifact"

    class FakeBuilder:
        max_steps = 1

        def __init__(self):
            self.calls = 0

        def cancel(self):
            pass

        async def run(self, prompt):
            self.calls += 1
            (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")
            yield AgentEvent(type="done", step=1, data={"result": "attempt complete"})

    class FakeRuntime:
        backend = "fake"

        def __init__(self):
            self.calls = 0

        async def verify(self):
            self.calls += 1
            if self.calls == 1:
                return VerificationReport(
                    passed=False,
                    checks={"runtimeBackend": "fake"},
                    errors=["[python_dependency_or_import] No module named scipy"],
                )
            return VerificationReport(
                passed=True,
                checks={
                    "runtimeBackend": "fake",
                    "registeredTools": sorted(plan.tool_names),
                    "smokeTestCount": 2,
                },
            )

    builder = FakeBuilder()
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "micro_agent.packaging.workflow._build_builder_agent",
        lambda *args, **kwargs: builder,
    )
    workflow = AgenticPackagingWorkflow(
        project_dir=project,
        ir=ir,
        artifact_dir=artifact,
        plan=plan,
        max_repairs=2,
        runtime_verifier_factory=lambda *_: runtime,
    )

    events = [event async for event in workflow.run("package it")]

    assert events[-1].type == "done"
    assert builder.calls == 2
    assert runtime.calls == 2
    marker = json.loads((artifact / ".ioeb-ready").read_text(encoding="utf-8"))
    assert marker["validationMode"] == "static_and_container_runtime"
    assert marker["runtimeVerified"] is True
    assert marker["runtimeBackend"] == "fake"
    assert marker["repairAttempts"] == 1
    assert marker["staticRepairAttempts"] == 0
    assert marker["runtimeRepairAttempts"] == 1
    assert marker["functionalVerified"] is False
    assert marker["readinessLevel"] == "structural_runtime"


async def test_static_failure_does_not_consume_runtime_repair_budget(tmp_path, monkeypatch):
    project = _sample_project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    plan = _plan(ir)
    artifact = tmp_path / "artifact"

    class FakeBuilder:
        max_steps = 1

        def __init__(self):
            self.calls = 0

        def cancel(self):
            pass

        async def run(self, prompt):
            self.calls += 1
            if self.calls == 1:
                (artifact / "server.py").write_text("def broken(:\n", encoding="utf-8")
            else:
                (artifact / "server.py").write_text(_valid_server(), encoding="utf-8")
            (artifact / "adapters.py").write_text(_valid_adapters(), encoding="utf-8")
            yield AgentEvent(type="done", step=1, data={"result": "attempt complete"})

    class FakeRuntime:
        backend = "fake"

        def __init__(self):
            self.calls = 0

        async def verify(self):
            self.calls += 1
            if self.calls == 1:
                return VerificationReport(
                    passed=False,
                    checks={"runtimeBackend": "fake"},
                    errors=["[source_import] cannot import name legacy_symbol"],
                )
            return VerificationReport(
                passed=True,
                checks={
                    "runtimeBackend": "fake",
                    "registeredTools": sorted(plan.tool_names),
                    "smokeTestCount": 2,
                    "functionalVerified": True,
                },
            )

    builder = FakeBuilder()
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "micro_agent.packaging.workflow._build_builder_agent",
        lambda *args, **kwargs: builder,
    )
    workflow = AgenticPackagingWorkflow(
        project_dir=project,
        ir=ir,
        artifact_dir=artifact,
        plan=plan,
        max_repairs=1,
        max_runtime_repairs=1,
        runtime_verifier_factory=lambda *_: runtime,
    )

    events = [event async for event in workflow.run("package it")]

    assert events[-1].type == "done"
    assert builder.calls == 3
    assert runtime.calls == 2
    marker = json.loads((artifact / ".ioeb-ready").read_text(encoding="utf-8"))
    assert marker["repairAttempts"] == 2
    assert marker["staticRepairAttempts"] == 1
    assert marker["runtimeRepairAttempts"] == 1
    assert marker["functionalVerified"] is True
    assert marker["readinessLevel"] == "functional"
