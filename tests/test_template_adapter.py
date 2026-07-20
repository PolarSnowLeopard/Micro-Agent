from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from micro_agent.packaging import template_adapter
from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.template_adapter import (
    PatchTemplateFile,
    ReadTemplateFile,
    StageProjectFixture,
    WriteTemplateFile,
    _runtime_requirement_errors,
    build_template_adapter_agent,
    validate_algorithm_template,
    verify_template_contract_runtime,
)
from scripts.prepare_amq_template_subset import (
    acquire_output_lock,
    _candidate_requires_replan,
    _compose_evaluation_benchmark,
    _is_l0,
    _save_template_snapshot,
    _restore_staged_project_fixtures,
    _template_repair_needs_source,
    _template_runtime_repair_advice,
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


def test_template_contract_records_uncovered_dispatch_branches(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float, factor: float) -> float:\n"
        "    return value * factor\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(mode: str, value: float) -> dict[str, float]:
    """Run a selected repository capability.

    Args:
        mode: Either double or triple.
        value: Input value.

    Returns:
        Computed result.
    """
    if mode == "double":
        result = run(value, 2)
    elif mode == "triple":
        result = run(value, 3)
    else:
        raise ValueError("unsupported mode")
    return {"result": result}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    contract = tests / "test_template_contract.py"
    contract.write_text(
        '''import pytest
from main import main_process

def test_double_contract():
    result = main_process(mode="double", value=2.0)
    assert result["result"] == 4.0

def test_triple_contract():
    result = main_process(mode="triple", value=2.0)
    assert result["result"] == 6.0

def test_invalid_mode_contract():
    with pytest.raises(ValueError):
        main_process(mode="invalid", value=2.0)
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert report.passed, report.to_json()
    assert report.checks["contractTestCallsMainProcess"] is True
    assert report.checks["contractBranchCoverage"] is True
    assert [
        (item["input"]["mode"], item["expectedOutcome"])
        for item in report.checks["contractFixtures"]
    ] == [
        ("double", "success"),
        ("triple", "success"),
        ("invalid", "error"),
    ]
    assert report.checks["contractSuccessFixtureCount"] == 2
    assert report.checks["contractOperationCounts"] == {"mode": 2}

    contract.write_text(
        '''from main import main_process

def test_double_contract():
    result = main_process(mode="double", value=2.0)
    assert result["result"] == 4.0
''',
        encoding="utf-8",
    )
    partial = validate_algorithm_template(project, require_contract_test=True)
    assert partial.passed, partial.to_json()
    assert partial.checks["contractBranchCoverage"] is False
    assert partial.checks["contractUncoveredBranches"] == ["mode='triple'"]


def test_template_contract_rejects_unconsumed_pytest_fixture(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n"
        "    return value * 2\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> dict[str, float]:
    """Run the repository function.

    Args:
        value: Numeric input.

    Returns:
        Computed result.
    """
    return {"result": run(value)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    contract = tests / "test_template_contract.py"
    contract.write_text(
        '''import pytest
from main import main_process

@pytest.fixture
def hidden_success():
    result = main_process(value=2.0)
    assert result["result"] == 4.0
    return result

def test_collection_only():
    assert True
''',
        encoding="utf-8",
    )

    rejected = validate_algorithm_template(project, require_contract_test=True)

    assert not rejected.passed
    assert rejected.checks["contractSuccessFixtureCount"] == 0
    assert rejected.checks["contractUncollectedCallCount"] == 1
    assert any("不会被 pytest/unittest 收集执行" in error for error in rejected.errors)

    contract.write_text(
        contract.read_text(encoding="utf-8")
        + "\ndef test_uses_fixture(hidden_success):\n"
        + '    assert hidden_success["result"] == 4.0\n',
        encoding="utf-8",
    )
    accepted = validate_algorithm_template(project, require_contract_test=True)

    assert accepted.passed, accepted.to_json()
    assert accepted.checks["contractSuccessFixtureCount"] == 1
    assert accepted.checks["contractUncollectedCallCount"] == 0


def test_template_contract_rejects_dynamic_or_network_dependent_fixtures(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n    return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> dict[str, float]:
    """Run the repository function.

    Args:
        value: Input value.

    Returns:
        Computed result.
    """
    return {"result": run(value)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''import requests
from main import main_process

VALUE = 2.0

def test_contract():
    result = main_process(value=VALUE)
    assert result["result"] == 2.0
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert not report.passed
    assert any("禁止导入: requests" in error for error in report.errors)
    assert any("必须是可审计的 JSON 字面量" in error for error in report.errors)
    assert len(
        [
            error
            for error in report.errors
            if "必须是可审计的 JSON 字面量" in error
        ]
    ) == 1


def test_template_contract_accepts_same_test_local_json_literal(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(values: list[float]) -> float:\n    return sum(values)\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(values: list[float]) -> dict[str, float]:
    """Run the repository function.

    Args:
        values: Numeric inputs.

    Returns:
        Computed result.
    """
    return {"result": run(values)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def test_contract():
    values = [1.0, 2.0, 3.0]
    result = main_process(values=values)
    assert result["result"] == 6.0
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert report.passed, report.to_json()
    assert report.checks["contractFixtures"][0]["input"] == {
        "values": [1.0, 2.0, 3.0]
    }
    assert report.checks["contractStaticBindingCount"] == 1

    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def make_values():
    return [1.0, 2.0, 3.0]

def test_contract():
    values = make_values()
    result = main_process(values=values)
    assert result["result"] == 6.0
''',
        encoding="utf-8",
    )

    rejected = validate_algorithm_template(project, require_contract_test=True)

    assert not rejected.passed
    assert any(
        "必须是可审计的 JSON 字面量" in error
        for error in rejected.errors
    )


def test_template_contract_defers_dynamic_json_input_to_isolated_runtime(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(values: list[float]) -> float:\n    return sum(values)\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(values: list[float]) -> dict[str, float]:
    """Run the repository function.

    Args:
        values: Numeric inputs.

    Returns:
        Computed result.
    """
    return {"result": run(values)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def make_values():
    return [1.0, 2.0, 3.0]

def test_contract():
    result = main_process(values=make_values())
    assert result["result"] == 6.0
''',
        encoding="utf-8",
    )

    static_only = validate_algorithm_template(
        project,
        require_contract_test=True,
    )
    runtime_deferred = validate_algorithm_template(
        project,
        require_contract_test=True,
        allow_runtime_collected_contract=True,
    )

    assert not static_only.passed
    assert runtime_deferred.passed, runtime_deferred.to_json()
    assert runtime_deferred.checks["contractTestCallsMainProcess"] is True
    assert runtime_deferred.checks["contractRuntimeCollectionRequired"] is True
    assert runtime_deferred.checks["contractFixtures"] == []


def test_template_contract_rejects_random_and_duplicate_success_fixtures(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(values: list[float]) -> float:\n    return sum(values)\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(values: list[float]) -> dict[str, float]:
    """Run the repository function.

    Args:
        values: Numeric inputs.

    Returns:
        Computed result.
    """
    return {"result": run(values)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''import numpy as np
from main import main_process

def test_first_contract():
    values = np.random.randn(4).tolist()
    result = main_process(values=values)
    assert isinstance(result["result"], float)

def test_duplicate_contract():
    result = main_process(values=[1.0, 2.0])
    assert result["result"] == 3.0
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(
        project,
        require_contract_test=True,
        allow_runtime_collected_contract=True,
        max_contract_success_fixtures=1,
    )

    assert not report.passed
    assert report.checks["contractNondeterministicCallLines"] == [5]
    assert any("禁止 random/numpy.random" in error for error in report.errors)
    assert any("只允许一个最小成功 fixture" in error for error in report.errors)


def test_runtime_contract_capture_parses_successful_json_inputs() -> None:
    payload = {
        "records": [
            {
                "line": 12,
                "input": {"values": [1.0, 2.0]},
                "expectedOutcome": "success",
            }
        ],
        "rejected": [],
        "truncated": False,
    }

    parsed = template_adapter._parse_runtime_contract_capture(
        "pytest output\n"
        + template_adapter._CONTRACT_FIXTURE_MARKER
        + json.dumps(payload)
        + "\n"
    )

    assert parsed == payload
    assert template_adapter._without_runtime_contract_capture(
        "failure details\n"
        + template_adapter._CONTRACT_FIXTURE_MARKER
        + json.dumps(payload)
        + "\nsummary"
    ) == (
        "failure details\nsummary\n"
        "[contract_fixture_capture] omitted 1 structured payload"
    )


def test_runtime_contract_capture_executes_dynamic_contract(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(values: list[float]) -> float:\n    return sum(values)\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(values: list[float]) -> dict[str, float]:
    """Run the repository function.

    Args:
        values: Numeric inputs.

    Returns:
        Computed result.
    """
    return {"result": run(values)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def make_values():
    return [1.0, 2.0, 3.0]

def test_contract():
    result = main_process(values=make_values())
    assert result["result"] == 6.0
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            template_adapter.sys.executable,
            "-c",
            template_adapter._CONTRACT_CAPTURE_RUNNER,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=30,
    )
    captured = template_adapter._parse_runtime_contract_capture(
        completed.stdout
    )

    assert completed.returncode == 0, completed.stderr
    assert captured is not None
    assert captured["records"] == [
        {
            "line": 7,
            "input": {"values": [1.0, 2.0, 3.0]},
            "expectedOutcome": "success",
        }
    ]


def test_template_contract_accepts_bounded_json_expressions_and_ignores_extra_dynamic_calls(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(values: list[float]) -> float:\n    return sum(values)\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(values: list[float]) -> dict[str, float]:
    """Run the repository function.

    Args:
        values: Numeric inputs.

    Returns:
        Computed result.
    """
    return {"result": run(values)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def make_values():
    return [4.0]

def test_compact_contract():
    values = [1.0] * (2 * 3)
    result = main_process(values=values)
    assert result["result"] == 6.0

def test_additional_dynamic_contract():
    result = main_process(values=make_values())
    assert result["result"] == 4.0
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert report.passed, report.to_json()
    assert report.checks["contractFixtures"][0]["input"] == {
        "values": [1.0] * 6
    }
    assert report.checks["contractDynamicInputCallCount"] == 1
    assert report.checks["contractStaticBindingCount"] == 1


def test_template_contract_accepts_unittest_assertion_methods(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n    return value * 2\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> dict[str, float]:
    """Run the repository function.

    Args:
        value: Input value.

    Returns:
        Computed result.
    """
    return {"result": run(value)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''import unittest
from main import main_process

class ContractTest(unittest.TestCase):
    def test_contract(self):
        result = main_process(value=2.0)
        self.assertEqual(result["result"], 4.0)
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert report.passed, report.to_json()
    assert report.checks["contractTestAssertions"] is True


def test_error_fixture_does_not_satisfy_success_output_assertion(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n"
        "    if value < 0:\n"
        "        raise ValueError('negative')\n"
        "    return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> dict[str, float]:
    """Run the repository function.

    Args:
        value: Numeric value.

    Returns:
        Computed value.
    """
    return {"value": run(value)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''import unittest
from main import main_process

class TestContract(unittest.TestCase):
    def test_success_without_output_assertion(self):
        main_process(value=1.0)

    def test_expected_error(self):
        with self.assertRaises(ValueError):
            main_process(value=-1.0)
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert not report.passed
    assert report.checks["contractSuccessFixtureCount"] == 1
    assert any(
        "每个成功模板契约 fixture" in error
        for error in report.errors
    )


def test_template_validator_rejects_dynamic_execution_in_reachable_helper(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n    return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def _evaluate(expression: str) -> object:
    return eval(expression)

def main_process(expression: str) -> dict[str, object]:
    """Evaluate an expression.

    Args:
        expression: User expression.

    Returns:
        Computed result.
    """
    run(1.0)
    return {"result": _evaluate(expression)}
''',
    )

    report = validate_algorithm_template(project)

    assert not report.passed
    assert report.checks["noDynamicCodeExecution"] is False
    assert any("禁止动态执行用户文本: eval" in error for error in report.errors)


def test_template_validator_rejects_overbroad_parameter_set(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: int) -> int:\n    return value\n",
        encoding="utf-8",
    )
    parameters = ", ".join(f"value_{index}: int = {index}" for index in range(13))
    doc_parameters = "\n".join(
        f"        value_{index}: Input {index}." for index in range(13)
    )
    project = _project(
        tmp_path,
        f'''from algorithm import run

def main_process({parameters}) -> dict[str, int]:
    """Run one repository operation.

    Args:
{doc_parameters}

    Returns:
        Computed result.
    """
    return {{"result": run(value_0)}}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    contract_cases = "\n\n".join(
        f'''def test_contract_{index}():
    result = main_process(value_0={index})
    assert result["result"] == {index}'''
        for index in range(13)
    )
    (tests / "test_template_contract.py").write_text(
        "from main import main_process\n\n" + contract_cases + "\n",
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert not report.passed
    assert report.checks["interfaceParameterCount"] == 13
    assert report.checks["contractFixtureBudget"] is True
    assert any("显式参数过多" in error for error in report.errors)
    assert _candidate_requires_replan(project, report)


def test_template_validator_replans_server_path_interfaces(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(values: list[float]) -> float:\n    return sum(values)\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(
    threshold: float,
    feature_files: list[str],
    checkpoint_dir: str,
) -> dict[str, float]:
    """Run a repository capability.

    Args:
        threshold: Numeric decision threshold.
        feature_files: File paths pointing to feature tensors.
        checkpoint_dir: Directory containing model checkpoints.

    Returns:
        Computed result.
    """
    return {"result": run([threshold, float(len(feature_files)), float(len(checkpoint_dir))])}
''',
    )

    report = validate_algorithm_template(project)

    assert not report.passed
    assert report.checks["serverPathParameters"] == [
        "feature_files",
        "checkpoint_dir",
    ]
    assert report.checks["noServerPathInterface"] is False
    assert any("容器内路径" in error for error in report.errors)
    assert _candidate_requires_replan(project, report)


def test_template_validator_accepts_content_transport_parameters(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: str) -> int:\n    return len(value)\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(
    features_base64: str,
    checkpoint_zip: str,
) -> dict[str, int]:
    """Run a repository capability.

    Args:
        features_base64: Base64-encoded feature tensor content.
        checkpoint_zip: Base64-encoded ZIP checkpoint content.

    Returns:
        Computed result.
    """
    return {"result": run(features_base64 + checkpoint_zip)}
''',
    )

    report = validate_algorithm_template(project)

    assert report.passed, report.to_json()
    assert report.checks["serverPathParameters"] == []
    assert report.checks["noServerPathInterface"] is True


def test_template_validator_rejects_success_error_control_envelopes(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n    return value * 2\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> dict[str, object]:
    """Run a repository capability.

    Args:
        value: Numeric input.

    Returns:
        Wrapped result.
    """
    try:
        return {"success": True, "operation": "predict", "result": run(value)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
''',
    )

    report = validate_algorithm_template(project)

    assert not report.passed
    assert report.checks["controlEnvelopeReturnLines"] == [13, 15]
    assert report.checks["noControlEnvelopeReturns"] is False
    assert any("控制信封" in error for error in report.errors)
    assert _candidate_requires_replan(project, report)


def test_template_validator_rejects_too_many_distinct_operations(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: int) -> int:\n    return value\n",
        encoding="utf-8",
    )
    branches = "\n".join(
        (
            f'    {"if" if index == 0 else "elif"} operation == "op{index}":\n'
            f"        result = run(value + {index})"
        )
        for index in range(9)
    )
    project = _project(
        tmp_path,
        f'''from algorithm import run

def main_process(operation: str, value: int) -> dict[str, int]:
    """Run one selected operation.

    Args:
        operation: Operation name.
        value: Input value.

    Returns:
        Computed result.
    """
{branches}
    else:
        raise ValueError("unknown operation")
    return {{"result": result}}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    contract_cases = "\n\n".join(
        f'''def test_contract_{index}():
    result = main_process(operation="op{index}", value=1)
    assert result["result"] == {index + 1}'''
        for index in range(9)
    )
    (tests / "test_template_contract.py").write_text(
        "from main import main_process\n\n" + contract_cases + "\n",
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert not report.passed
    assert report.checks["contractOperationCounts"] == {"operation": 9}
    assert any("operation 过多" in error for error in report.errors)
    assert _candidate_requires_replan(project, report)


def test_template_contract_rejects_selector_that_only_validates_and_echoes(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: int) -> int:\n    return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(operation: str, value: int) -> dict[str, object]:
    """Run one selected operation.

    Args:
        operation: Claimed operation name.
        value: Integer input.

    Returns:
        Computed result.
    """
    if operation not in ("first", "second"):
        raise ValueError("unknown operation")
    return {"operation": operation, "result": run(value)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def test_first_contract():
    result = main_process(operation="first", value=1)
    assert result["result"] == 1

def test_second_contract():
    result = main_process(operation="second", value=2)
    assert result["result"] == 2
''',
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert not report.passed
    assert report.checks["contractOperationCounts"] == {"operation": 2}
    assert any(
        "[contract_selector_semantics]" in error
        for error in report.errors
    )


def test_fixture_budget_error_preserves_repairable_complete_candidate(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: int) -> int:\n    return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: int) -> dict[str, int]:
    """Run the repository operation.

    Args:
        value: Integer input.

    Returns:
        Computed result.
    """
    return {"result": run(value)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    contract_cases = "\n\n".join(
        f'''def test_contract_{index}():
    result = main_process(value={index})
    assert result["result"] == {index}'''
        for index in range(31)
    )
    (tests / "test_template_contract.py").write_text(
        "from main import main_process\n\n" + contract_cases + "\n",
        encoding="utf-8",
    )

    report = validate_algorithm_template(project, require_contract_test=True)

    assert not report.passed
    assert report.checks["contractFixtureBudget"] is False
    assert any("fixture 过多" in error for error in report.errors)
    assert not _candidate_requires_replan(project, report)


def test_contract_runtime_rejects_non_reproducible_requirements(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "--extra-index-url https://example.test/simple\n"
        "valid-package>=1\n"
        "checkout @ git+https://example.test/private/repo.git\n",
        encoding="utf-8",
    )

    errors = _runtime_requirement_errors(requirements)

    assert len(errors) == 2
    assert any("pip 命令行选项" in error for error in errors)
    assert any("URL/VCS/本地路径依赖" in error for error in errors)


def test_contract_runtime_executes_in_restricted_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n    return value * 2\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> dict[str, float]:
    """Run the repository algorithm.

    Args:
        value: Input value.

    Returns:
        Computed result.
    """
    return {"result": run(value)}
''',
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def test_contract():
    result = main_process(value=2.0)
    assert result["result"] == 4.0
''',
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    dockerfiles: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:2] == ["docker", "build"]:
            dockerfiles.extend(
                path.read_text(encoding="utf-8")
                for path in project.glob(".ioeb-template-contract-*.Dockerfile")
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(template_adapter.subprocess, "run", fake_run)

    report = verify_template_contract_runtime(project)

    assert report.passed, report.to_json()
    docker_run = next(command for command in commands if command[:2] == ["docker", "run"])
    assert "--network" in docker_run and "none" in docker_run
    assert "--read-only" in docker_run
    assert "--cap-drop" in docker_run and "ALL" in docker_run
    assert "PYTHONPATH=/workspace:/workspace/src:/ioeb" in docker_run
    assert "HOME=/tmp" in docker_run
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in docker_run
    assert "MPLCONFIGDIR=/tmp/matplotlib" in docker_run
    assert "XDG_CACHE_HOME=/tmp/cache" in docker_run
    assert "IOEB_CONTRACT_ROOT=/ioeb" in docker_run
    assert '"-c",\n            "/dev/null"' in docker_run[-1]
    assert "--confcutdir={_contract_root}" in docker_run[-1]
    assert docker_run[docker_run.index("--workdir") + 1] == "/workspace"
    assert docker_run[docker_run.index("--entrypoint") + 1] == "python"
    assert template_adapter._CONTRACT_FIXTURE_MARKER in docker_run[-1]
    assert len(dockerfiles) == 1
    assert "--mount=type=cache,target=/root/.cache/pip,sharing=locked" in dockerfiles[0]
    assert "--no-cache-dir" not in dockerfiles[0]
    assert "PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu" in dockerfiles[0]
    assert "/tmp/requirements-cpu.txt" in dockerfiles[0]
    assert "/tmp/requirements-main.txt" in dockerfiles[0]
    assert "libexpat1 libgomp1 libgl1 libglib2.0-0" in dockerfiles[0]
    assert "libxrender1 libxext6 libsm6" in dockerfiles[0]
    assert "libopenslide0" in dockerfiles[0]
    assert '"-c", "/dev/null"' in dockerfiles[0]
    assert not list(project.glob(".ioeb-template-contract-*"))


def test_contract_runtime_records_installed_distribution_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "compiledlib"
    package.mkdir()
    (package / "__init__.py").write_text(
        "def run(value: float) -> float:\n    return value * 2\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from compiledlib import run

def main_process(value: float) -> dict[str, float]:
    """Run the compiled library operation.

    Args:
        value: Input value.

    Returns:
        Computed result.
    """
    return {"result": run(value)}
''',
    )
    (project / "requirements.txt").write_text(
        "compiledlib>=1\n",
        encoding="utf-8",
    )
    tests = project / "tests_ioeb"
    tests.mkdir()
    (tests / "test_template_contract.py").write_text(
        '''from main import main_process

def test_contract():
    result = main_process(value=2.0)
    assert result["result"] == 4.0
''',
        encoding="utf-8",
    )
    docker_runs = 0
    docker_commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal docker_runs
        if command[:2] == ["docker", "run"]:
            docker_runs += 1
            docker_commands.append(command)
            if docker_runs == 1:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr=(
                        "ImportError: compiled extension missing "
                        "(/workspace/compiledlib/__init__.py)"
                    ),
                )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(template_adapter.subprocess, "run", fake_run)

    report = verify_template_contract_runtime(project)

    assert report.passed, report.to_json()
    assert docker_runs == 2
    assert report.checks["executionMode"] == "installed_distribution_fallback"
    assert report.checks["sourceTestExitCode"] == 1
    assert report.checks["installedDistributionTestExitCode"] == 0
    assert report.checks["installedDistributionFallbackCandidates"] == [
        "compiledlib"
    ]
    fallback_command = docker_commands[-1]
    assert fallback_command[fallback_command.index("--workdir") + 1] == "/ioeb"
    assert any("同名发行包" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_template_writer_creates_only_reviewed_contract_path(
    tmp_path: Path,
) -> None:
    writer = WriteTemplateFile(tmp_path)

    result = await writer.execute(
        path="tests_ioeb/test_template_contract.py",
        content="from main import main_process\n",
    )
    rejected = await writer.execute(
        path="tests_ioeb/arbitrary.py",
        content="raise RuntimeError\n",
    )

    assert not result.error
    assert (tmp_path / "tests_ioeb/test_template_contract.py").is_file()
    assert rejected.error


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


def test_template_validator_allows_package_star_reexports(tmp_path: Path) -> None:
    package = tmp_path / "solver"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .core import *\n",
        encoding="utf-8",
    )
    (package / "core.py").write_text(
        "def run(value: float) -> float:\n    return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from solver import run

def main_process(value: float) -> float:
    """Run a re-exported repository function.

    Args:
        value: Input value.

    Returns:
        Solver output.
    """
    return run(value)
''',
    )

    report = validate_algorithm_template(project)

    assert report.passed, report.to_json()
    assert report.checks["resolvableRepositoryImports"] is True


def test_template_validator_allows_conditionally_defined_module_member(
    tmp_path: Path,
) -> None:
    package = tmp_path / "solver"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(
        "if True:\n"
        "    def conditional_run(value: float) -> float:\n"
        "        return value\n",
        encoding="utf-8",
    )
    project = _project(
        tmp_path,
        '''from solver.core import conditional_run

def main_process(value: float) -> float:
    """Run a conditionally exposed repository function.

    Args:
        value: Input value.

    Returns:
        Solver output.
    """
    return conditional_run(value)
''',
    )

    report = validate_algorithm_template(project)

    assert report.passed, report.to_json()
    assert report.checks["resolvableRepositoryImports"] is True


def test_template_repair_agent_uses_candidate_context_instead_of_rescanning(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithm.py").write_text(
        "def run(value: float) -> float:\n    return value\n",
        encoding="utf-8",
    )
    _project(
        tmp_path,
        '''from algorithm import run

def main_process(value: float) -> float:
    """Run the repository function.

    Args:
        value: Input value.

    Returns:
        Output value.
    """
    return run(value)
''',
    )
    ir = RepositoryAnalyzer().analyze(tmp_path)

    agent = build_template_adapter_agent(tmp_path, ir, repair=True)

    assert agent.tools.get("inspect_repository") is None
    assert agent.tools.get("read_project_file").max_reads == 4
    assert agent.tools.get("read_template_file") is not None
    assert agent.tools.get("patch_template_file") is not None
    assert agent.tools.get("stage_project_fixture") is not None
    assert agent.tools.get("terminate") is None
    assert agent.max_steps == 16
    assert agent.require_terminal_tool is True
    assert agent.duplicate_tool_retry_limit == 4
    assert agent.terminal_tools == {"verify_template"}
    assert "当前 main.py" in agent.system_prompt

    local_only = build_template_adapter_agent(
        tmp_path,
        ir,
        repair=True,
        repair_source_reads=False,
    )
    assert local_only.tools.get("read_project_file") is None
    assert local_only.tools.get("read_template_file") is not None
    assert local_only.tools.get("stage_project_fixture") is None
    assert "源码读取工具已关闭" in local_only.system_prompt
    assert not _template_repair_needs_source(
        [
            "main_process 必须使用 Google 风格 docstring",
            "模板契约 fixture 过多",
        ]
    )
    assert not _template_repair_needs_source(
        [
            "每个已发现能力只允许一个最小成功 fixture；"
            "当前 success_calls=4, capabilities=1"
        ]
    )
    assert _template_repair_needs_source(
        ["AttributeError: module has no attribute 'run'"]
    )
    object_advice = _template_runtime_repair_advice(
        [
            "ValueError: Symbol q_0(t) is not a state.",
            "SympifyError: could not parse formatted expression",
        ]
    )
    assert "不要用 sympify" in object_advice
    assert "映射回该集合中的原对象" in object_advice
    assert "格式化结果被错误地重新解析" in object_advice
    empty_result_advice = _template_runtime_repair_advice(
        [
            "AssertionError: assert 'trait' in {}",
            "KeyError: 'trait'",
        ]
    )
    assert "canonical vocabulary" in empty_result_advice
    deprecation_advice = _template_runtime_repair_advice(
        [
            "DeprecationWarning: LegacyProcessor is deprecated. "
            "Please use ProcessingPipeline instead."
        ]
    )
    assert "search_project_text" in deprecation_advice
    assert "替代" in deprecation_advice
    datapoint_advice = _template_runtime_repair_advice(
        [
            "The given datapoint does not have an 'ecg' attribute.",
            "The passed object does not seem to be a EcgRawDataFrame.",
        ]
    )
    assert "SimpleNamespace" in datapoint_advice
    assert ".sampling_rate_hz" in datapoint_advice
    numpy_advice = _template_runtime_repair_advice(
        ["AttributeError: module 'numpy' has no attribute 'trapz'"]
    )
    assert "numpy>=1.26,<2" in numpy_advice
    assert "monkeypatch" in numpy_advice
    assert "禁止伪造返回键" in empty_result_advice
    literal_advice = _template_runtime_repair_advice(
        ["模板契约测试的 main_process 输入必须是可审计的 JSON 字面量"]
    )
    assert "位置参数和关键字值必须直接写成" in literal_advice
    assert "同一测试函数内的局部变量" in literal_advice
    assert "允许由常量组成的 list/string 拼接和重复" in literal_advice
    assert "不能在测试中临时生成随机模型" in literal_advice
    envelope_advice = _template_runtime_repair_advice(
        ['assert result["success"] is True']
    )
    assert "异常被控制信封吞掉" in envelope_advice
    assert "保留原异常" in envelope_advice
    invariant_advice = _template_runtime_repair_advice(
        ["AssertionError: assert 56 == 50"]
    )
    assert "可由 fixture/输入推导的领域不变量" in invariant_advice
    reaction_advice = _template_runtime_repair_advice(
        [
            "TypeError: '<' not supported between instances of 'Species'",
            "ValueError: Unknown key: A",
        ]
    )
    assert "稳定字符串标识符" in reaction_advice
    equivalency_advice = _template_runtime_repair_advice(
        ["ValueError: Invalid equivalence entry 0"]
    )
    assert "不能再次包装" in equivalency_advice
    dependency_advice = _template_runtime_repair_advice(
        ["ERROR: No matching distribution found for optional-vis-package"]
    )
    assert "禁止把原仓库的 Git/VCS 依赖猜成" in dependency_advice
    assert "未使用的 import" in dependency_advice
    missing_module_advice = _template_runtime_repair_advice(
        ["ModuleNotFoundError: No module named 'composer'"]
    )
    assert "真实发行包名" in missing_module_advice
    assert "更具体子模块" in missing_module_advice
    assert "不能只补 traceback 中第一个包" in missing_module_advice
    assert "一次性比对" in missing_module_advice
    module_state_advice = _template_runtime_repair_advice(
        ["禁止模块级调用初始化运行状态，相关行: 30"]
    )
    assert "模型构造、数据加载" in module_state_advice
    runtime_advice = _template_runtime_repair_advice(
        [
            "[contract_test] 4 failed, 8 passed",
            "RuntimeError: max_pool1d Invalid computed output size: 0",
            "URLError: Temporary failure in name resolution",
            "ParseException: invalid formula",
        ]
    )
    assert "不必穷举" in runtime_advice
    assert "逐维记录输入和输出" in runtime_advice
    assert "隔离运行触发联网下载" in runtime_advice
    assert "真实可解析的最小输入" in runtime_advice
    paired_advice = _template_runtime_repair_advice(
        ["ValueError: Dataframe has less rows than non-null values in columns. "
         "ds and y must have the same length"]
    )
    assert "并行输入长度不一致" in paired_advice
    assert "静默截断" in paired_advice
    fixture_budget_advice = _template_runtime_repair_advice(
        [
            "每个已发现能力只允许一个最小成功 fixture；"
            "当前 success_calls=4, capabilities=1"
        ]
    )
    assert "只保留一个最能覆盖 CapabilityDesign" in fixture_budget_advice
    assert "只改 tests_ioeb/test_template_contract.py" in fixture_budget_advice


@pytest.mark.asyncio
async def test_stage_project_fixture_copies_bounded_asset_and_records_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tests" / "data" / "sample.tif"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"II*\x00binary-raster")
    tool = StageProjectFixture(tmp_path)

    result = await tool.execute(
        source_path="tests/data/sample.tif",
        fixture_name="raster-input.tif",
    )

    assert result.error is None
    destination = tmp_path / "tests_ioeb/fixtures/raster-input.tif"
    assert destination.read_bytes() == source.read_bytes()
    manifest = json.loads(
        (tmp_path / "tests_ioeb/fixtures/.ioeb-fixtures.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["files"][0]["sourcePath"] == "tests/data/sample.tif"
    assert manifest["files"][0]["destinationPath"] == (
        "tests_ioeb/fixtures/raster-input.tif"
    )
    assert manifest["files"][0]["bytes"] == len(source.read_bytes())


@pytest.mark.asyncio
async def test_stage_project_fixture_rejects_traversal_and_oversize(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside.bin"
    outside.write_bytes(b"outside")
    tool = StageProjectFixture(tmp_path)
    traversal = await tool.execute(
        source_path="../outside.bin",
        fixture_name="outside.bin",
    )
    bad_name = await tool.execute(
        source_path="../outside.bin",
        fixture_name="../outside.bin",
    )
    large = tmp_path / "large.bin"
    large.write_bytes(b"\0" * (8 * 1024 * 1024 + 1))
    oversize = await tool.execute(
        source_path="large.bin",
        fixture_name="large.bin",
    )

    assert "路径越界" in traversal.error
    assert "安全文件名" in bad_name.error
    assert "8388608" in oversize.error


def test_staged_project_fixture_manifest_survives_snapshot_resume(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    (project / "tests/data").mkdir(parents=True)
    run_dir.mkdir()
    payload = b"fixture-payload"
    source = project / "tests/data/input.bin"
    source.write_bytes(payload)
    fixture_dir = project / "tests_ioeb/fixtures"
    fixture_dir.mkdir(parents=True)
    manifest = {
        "schemaVersion": "ioeb.template-fixtures/v1",
        "files": [
            {
                "sourcePath": "tests/data/input.bin",
                "destinationPath": "tests_ioeb/fixtures/input.bin",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    (fixture_dir / ".ioeb-fixtures.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _save_template_snapshot(project, run_dir)
    shutil_target = fixture_dir / "input.bin"
    shutil_target.unlink(missing_ok=True)

    recovered = recover_last_template_writes(run_dir)
    assert "tests_ioeb/fixtures/.ioeb-fixtures.json" in recovered
    assert _restore_staged_project_fixtures(project) == 1
    assert shutil_target.read_bytes() == payload


async def test_patch_template_file_requires_one_exact_match(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("VALUE = 1\nRESULT = VALUE\n", encoding="utf-8")
    tool = PatchTemplateFile(tmp_path)

    result = await tool.execute(
        path="main.py",
        old="VALUE = 1",
        new="VALUE = 2",
    )
    ambiguous = await tool.execute(
        path="main.py",
        old="VALUE",
        new="INPUT",
    )

    assert not result.error
    assert path.read_text(encoding="utf-8") == "VALUE = 2\nRESULT = VALUE\n"
    assert ambiguous.error
    assert "当前 2 次" in ambiguous.error
    assert "read_template_file" in ambiguous.error


async def test_read_template_file_returns_bounded_current_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tests_ioeb"
    path.mkdir()
    (path / "test_template_contract.py").write_text(
        "line1\nline2\nline3\nline4\n",
        encoding="utf-8",
    )
    tool = ReadTemplateFile(tmp_path, max_reads=1, max_lines=2)

    result = await tool.execute(
        path="tests_ioeb/test_template_contract.py",
        start_line=2,
        end_line=3,
    )
    exhausted = await tool.execute(path="main.py")

    assert not result.error
    assert "lines 2-3 (total 4)" in result.output
    assert result.output.endswith("line2\nline3")
    assert exhausted.error
    assert "读取次数已达上限" in exhausted.error


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


def test_output_lock_prevents_concurrent_adaptation_writers(
    tmp_path: Path,
) -> None:
    output = tmp_path / "derived"
    first = acquire_output_lock(output)
    try:
        with pytest.raises(RuntimeError, match="another adaptation process"):
            acquire_output_lock(output)
    finally:
        first.close()

    second = acquire_output_lock(output)
    second.close()


def test_evaluation_benchmark_keeps_failed_adaptations_in_denominator() -> None:
    original = [
        {
            "sample_id": "ready",
            "repo_info": {"url": "original-ready", "commit_sha": "a"},
            "task": "one",
        },
        {
            "sample_id": "failed",
            "repo_info": {"url": "original-failed", "commit_sha": "b"},
            "task": "two",
        },
    ]
    adapted = {
        "ready": {
            **original[0],
            "repo_info": {"url": "adapted-ready", "commit_sha": "c"},
        }
    }

    rows = _compose_evaluation_benchmark(original, adapted)

    assert len(rows) == 2
    assert rows[0]["repo_info"]["url"] == "adapted-ready"
    assert rows[1] == original[1]


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
        {
            "type": "tool_call",
            "data": {
                "tool": "patch_template_file",
                "arguments": {
                    "path": "main.py",
                    "old": "second",
                    "new": "patched",
                },
            },
        },
        {
            "type": "tool_call",
            "data": {
                "tool": "write_template_file",
                "arguments": {
                    "path": "tests_ioeb/test_template_contract.py",
                    "content": "contract",
                },
            },
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    assert recover_last_template_writes(tmp_path) == {
        "main.py": "patched\n",
        "tests_ioeb/test_template_contract.py": "contract\n",
    }


def test_template_snapshot_survives_patch_only_followup_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    project.mkdir()
    run_dir.mkdir()
    (project / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    (project / "tests_ioeb").mkdir()
    (project / "tests_ioeb" / "test_template_contract.py").write_text(
        "assert True\n",
        encoding="utf-8",
    )
    _save_template_snapshot(project, run_dir)
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_call",
                "data": {
                    "tool": "patch_template_file",
                    "arguments": {
                        "path": "main.py",
                        "old": "VALUE = 1",
                        "new": "VALUE = 2",
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert recover_last_template_writes(run_dir) == {
        "main.py": "VALUE = 2\n",
        "tests_ioeb/test_template_contract.py": "assert True\n",
    }
