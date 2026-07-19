from __future__ import annotations

import json
from pathlib import Path

import pytest

from micro_agent.packaging.template_adapter import validate_algorithm_template
from scripts.prepare_amq_template_subset import (
    _is_l0,
    ensure_output_outside_source_repo,
    load_mini30,
    recover_last_template_writes,
)


def _project(tmp_path: Path, main: str) -> Path:
    (tmp_path / "main.py").write_text(main, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    return tmp_path


def test_template_validator_requires_real_local_repository_call(tmp_path: Path) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def predict(value: float) -> float:\n    return value * 2\n", encoding="utf-8"
    )
    project = _project(
        tmp_path,
        '''from algorithm import predict

def _predict(value: float) -> float:
    return predict(value)

def main_process(value: float) -> dict[str, float]:
    """Run the real algorithm.

    Args:
        value: Input value.

    Returns:
        A JSON-compatible prediction.
    """
    return {"prediction": _predict(value)}
''',
    )

    report = validate_algorithm_template(project)

    assert report.passed
    assert report.checks["repositoryCallRoots"] == ["predict"]
    assert report.checks["reachableLocalFunctions"] == ["_predict", "main_process"]


def test_template_validator_rejects_hallucinated_local_import_members(
    tmp_path: Path,
) -> None:
    package = tmp_path / "solver"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(
        "class RealSolver:\n"
        "    def run(self, value: float) -> float:\n"
        "        return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from solver.core import ImaginedSolver

def main_process(value: float) -> float:
    """Run a repository solver.

    Args:
        value: Input value.

    Returns:
        Solver output.
    """
    return ImaginedSolver().run(value)
''',
    )

    report = validate_algorithm_template(project)

    assert not report.passed
    assert report.checks["resolvableRepositoryImports"] is False
    assert any(
        "solver.core.ImaginedSolver (main.py:1)" in error
        and "不能凭名称猜测" in error
        for error in report.errors
    )


def test_template_validator_rejects_stdlib_only_facade(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        '''import json

def main_process(payload: str) -> dict[str, object]:
    """Parse JSON without invoking repository code.

    Args:
        payload: JSON input.

    Returns:
        Parsed payload.
    """
    return json.loads(payload)
''',
    )

    report = validate_algorithm_template(project)

    assert not report.passed
    assert any("真实算法能力" in error for error in report.errors)


def test_template_validator_rejects_module_level_runtime_initialization(tmp_path: Path) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def load():\n    return object()\n\ndef run(model, value):\n    return value\n", encoding="utf-8"
    )
    project = _project(
        tmp_path,
        '''from algorithm import load, run

MODEL = load()

def main_process(value: float) -> float:
    """Run an algorithm.

    Args:
        value: Input value.

    Returns:
        Output value.
    """
    return run(MODEL, value)
''',
    )

    report = validate_algorithm_template(project)

    assert not report.passed
    assert any("模块级调用" in error for error in report.errors)


def test_template_validator_allows_pure_derived_module_constants_and_type_ellipsis(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(values):\n    return len(values)\n", encoding="utf-8"
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

LABELS = ["a", "b"]
LABEL_COUNT = len(LABELS)

def main_process(values: tuple[float, ...]) -> dict[str, int]:
    """Run an algorithm.

    Args:
        values: Input values.

    Returns:
        Output count.
    """
    return {"count": run(values), "labels": LABEL_COUNT}
''',
    )

    assert validate_algorithm_template(project).passed


def test_template_validator_accepts_dependency_proven_by_original_notebook(tmp_path: Path) -> None:
    notebook = {
        "cells": [
            {
                "cell_type": "code",
                "source": ["from transformers import AutoTokenizer\n"],
            }
        ]
    }
    (tmp_path / "tutorial.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
    project = _project(
        tmp_path,
        '''from transformers import AutoTokenizer

def main_process(model_name: str) -> dict[str, str]:
    """Load a tokenizer declared by the source notebook.

    Args:
        model_name: Model identifier.

    Returns:
        Loaded tokenizer class.
    """
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    return {"class": type(tokenizer).__name__}
''',
    )

    report = validate_algorithm_template(project)

    assert report.passed
    assert report.checks["repositoryEvidenceMode"] == "source_declared_dependency_call"
    assert report.checks["repositoryEvidenceModules"] == ["transformers"]


def test_template_validator_accepts_local_call_via_reachable_module_mapping(tmp_path: Path) -> None:
    package = tmp_path / "models"
    package.mkdir()
    (package / "__init__.py").write_text(
        "def build(value: float) -> float:\n    return value\n", encoding="utf-8"
    )
    project = _project(
        tmp_path,
        '''from models import build

MODEL_MAP = {"default": build}

def main_process(value: float) -> dict[str, float]:
    """Run the selected local model.

    Args:
        value: Input value.

    Returns:
        Model output.
    """
    factory = MODEL_MAP["default"]
    return {"result": factory(value)}
''',
    )

    report = validate_algorithm_template(project)

    assert report.passed
    assert report.checks["repositoryEvidenceMode"] == "local_module_call"


def test_template_validator_allows_pass_only_in_cleanup_exception(tmp_path: Path) -> None:
    (tmp_path / "algorithm.py").write_text("def run(value):\n    return value\n", encoding="utf-8")
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> float:
    """Run and clean up.

    Args:
        value: Input value.

    Returns:
        Output value.
    """
    try:
        result = run(value)
    except RuntimeError:
        pass
    return result
''',
    )

    assert validate_algorithm_template(project).passed


def test_l0_requires_explicit_negative_control_opt_in(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        '''def main_process(request: dict[str, object]) -> dict[str, object]:
    """Reject unsupported work.

    Args:
        request: Input request.

    Returns:
        This function never returns successfully.
    """
    raise RuntimeError("unsupported")
''',
    )

    assert not validate_algorithm_template(project).passed
    report = validate_algorithm_template(project, allow_explicit_unsupported=True)
    assert report.passed
    assert report.checks["explicitUnsupported"] is True


def test_mini30_loader_requires_exactly_30_unique_samples(tmp_path: Path) -> None:
    samples = [
        {
            "sample_id": f"sample-{index}",
            "wrap_intent": "wrap this capability",
            "repo_info": {"url": "https://example.test/repo", "commit_sha": "abc"},
        }
        for index in range(30)
    ]
    benchmark = tmp_path / "mini30.jsonl"
    benchmark.write_text(
        "".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8"
    )

    assert len(load_mini30(benchmark)) == 30
    benchmark.write_text(json.dumps(samples[0]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly 30"):
        load_mini30(benchmark)


def test_l0_detection_uses_official_repo_category() -> None:
    assert _is_l0({"repo_info": {"category": "L0_Infeasible"}})
    assert not _is_l0({"repo_info": {"category": "L3_Complex"}})


def test_output_guard_refuses_writing_inside_source_git_repository(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    benchmark = source / "mini30.jsonl"
    benchmark.write_text("", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "init", "--quiet"], cwd=source, check=True)

    with pytest.raises(ValueError, match="outside"):
        ensure_output_outside_source_repo(benchmark, source / "derived")


def test_recover_last_template_writes_uses_latest_agent_content(tmp_path: Path) -> None:
    events = [
        {
            "type": "tool_call",
            "data": {
                "tool": "write_template_file",
                "arguments": {"path": "main.py", "content": "first"},
            },
        },
        {"type": "think", "data": {"thought": "ignore"}},
        {
            "type": "tool_call",
            "data": {
                "tool": "write_template_file",
                "arguments": {"path": "main.py", "content": "second"},
            },
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    assert recover_last_template_writes(tmp_path) == {"main.py": "second\n"}
