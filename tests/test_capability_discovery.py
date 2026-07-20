from __future__ import annotations

import json
from pathlib import Path

import pytest

from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.discovery import (
    DISCOVERY_SCHEMA_VERSION,
    CapabilityDesign,
    CapabilityDesignStore,
    CapabilityDesignValidationError,
    _build_discovery_agent,
    capability_discovery_prompt,
)
from micro_agent.packaging.template_adapter import template_adapter_prompt
from micro_agent.packaging.workflow import _build_planning_agent, _planner_prompt
from micro_agent.packaging.tools import PlanStore


def _project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        "# Risk model\nUse predict for one score and evaluate for a labelled batch.\n",
        encoding="utf-8",
    )
    (tmp_path / "core.py").write_text(
        "def predict(value: float) -> dict[str, float]:\n"
        "    return {'score': value}\n\n"
        "def evaluate(values: list[float]) -> dict[str, float]:\n"
        "    return {'mean': sum(values) / len(values)}\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(
        "from core import predict\n\n"
        "def test_predict():\n"
        "    assert predict(0.5)['score'] == 0.5\n",
        encoding="utf-8",
    )
    return tmp_path


def _raw_design() -> dict:
    return {
        "schemaVersion": DISCOVERY_SCHEMA_VERSION,
        "decision": "design",
        "summary": "The repository exposes separate prediction and evaluation capabilities.",
        "capabilities": [
            {
                "name": "predict_risk",
                "description": (
                    "Predict one normalized risk score when an agent needs "
                    "a single observation assessment."
                ),
                "sourceSymbols": ["core.predict"],
                "sourceFiles": ["core.py"],
                "composition": "Call core.predict directly after validating a finite number.",
                "inputNotes": "Accept one finite floating point observation value.",
                "outputNotes": "Return the source score field as a JSON number.",
                "fixtureGuidance": (
                    "Reuse tests/test_core.py:4 with value 0.5 and assert score 0.5."
                ),
                "evidence": ["tests/test_core.py:4"],
            },
            {
                "name": "evaluate_risk_batch",
                "description": (
                    "Evaluate a batch of observations when an agent needs "
                    "an aggregate model quality result."
                ),
                "sourceSymbols": ["core.evaluate"],
                "sourceFiles": ["core.py"],
                "composition": "Call core.evaluate with the validated list of values.",
                "inputNotes": "Accept a non-empty list of finite floating point values.",
                "outputNotes": "Return the source mean field as a JSON number.",
                "fixtureGuidance": (
                    "Use the README example with a deterministic list and assert its mean."
                ),
                "evidence": ["README.md:2"],
            },
        ],
        "excludedSymbols": [],
        "risks": [],
    }


def test_capability_design_validates_source_evidence(tmp_path: Path) -> None:
    ir = RepositoryAnalyzer().analyze(_project(tmp_path))

    design = CapabilityDesign.validate(
        _raw_design(),
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )

    assert design.decision == "design"
    assert [item["name"] for item in design.capabilities] == [
        "predict_risk",
        "evaluate_risk_batch",
    ]


def test_capability_design_rejects_unknown_source_symbol(tmp_path: Path) -> None:
    ir = RepositoryAnalyzer().analyze(_project(tmp_path))
    raw = _raw_design()
    raw["capabilities"][0]["sourceSymbols"] = ["core.missing"]

    with pytest.raises(CapabilityDesignValidationError) as exc_info:
        CapabilityDesign.validate(
            raw,
            known_symbols=ir.known_symbols,
            known_files={file.path for file in ir.files},
        )

    assert "core.missing" in str(exc_info.value)


def test_capability_design_store_persists_only_valid_candidate(
    tmp_path: Path,
) -> None:
    ir = RepositoryAnalyzer().analyze(_project(tmp_path / "project"))
    destination = tmp_path / "run" / "capability_design.json"
    store = CapabilityDesignStore(
        path=destination,
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )

    result = store.save(json.dumps(_raw_design()))

    assert result.error is None
    assert destination.is_file()
    assert store.design is not None
    assert json.loads(destination.read_text(encoding="utf-8"))["schemaVersion"] == (
        DISCOVERY_SCHEMA_VERSION
    )


def test_discovery_agent_has_bounded_evidence_tools(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    store = CapabilityDesignStore(
        path=tmp_path / "capability_design.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )

    agent = _build_discovery_agent(project, ir, store)

    assert set(agent.tools.list_names()) == {
        "inspect_repository",
        "read_project_file",
        "search_project_text",
        "save_capability_design_json",
    }
    assert agent.terminal_tools == {"save_capability_design_json"}
    assert agent.require_terminal_tool is True
    assert agent.tools.get("read_project_file").max_reads == 10
    assert agent.tools.get("search_project_text").max_calls == 5


@pytest.mark.asyncio
async def test_discovery_search_finds_source_usage_but_not_generated_candidate(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    generated = project / "tests_ioeb"
    generated.mkdir()
    (generated / "test_template_contract.py").write_text(
        "predict(999)\n",
        encoding="utf-8",
    )
    ir = RepositoryAnalyzer().analyze(project)
    store = CapabilityDesignStore(
        path=tmp_path / "capability_design.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )
    search = _build_discovery_agent(project, ir, store).tools.get(
        "search_project_text"
    )

    result = await search.execute(query="predict")

    assert result.error is None
    assert "tests/test_core.py" in result.output
    assert "tests_ioeb/test_template_contract.py" not in result.output


def test_discovery_design_becomes_planner_and_template_input(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    ir = RepositoryAnalyzer().analyze(project)
    design = CapabilityDesign.validate(
        _raw_design(),
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )
    store = PlanStore(
        path=tmp_path / "packaging_plan.json",
        known_symbols=ir.known_symbols,
        known_files={file.path for file in ir.files},
    )

    discovery_prompt = capability_discovery_prompt(ir, "wrap risk scoring")
    planner_prompt = _planner_prompt(
        ir,
        "wrap risk scoring",
        capability_design=design,
    )
    adapter_prompt = template_adapter_prompt(
        ir,
        "wrap risk scoring",
        None,
        capability_design=design,
    )
    planner = _build_planning_agent(
        project,
        ir,
        store,
        capability_design=design,
    )

    assert '"method": "darp-bage/v1"' in discovery_prompt
    assert '"predict_risk"' in planner_prompt
    assert "不能无理由退化为单一 main_process 工具" in planner_prompt
    assert '"evaluate_risk_batch"' in adapter_prompt
    assert planner.tools.get("inspect_repository") is None
    assert planner.tools.get("read_project_file").max_reads == 4
