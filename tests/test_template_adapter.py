from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from micro_agent.packaging import template_adapter
from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.template_adapter import (
    PatchTemplateFile,
    ReadTemplateFile,
    WriteTemplateFile,
    _runtime_requirement_errors,
    build_template_adapter_agent,
    validate_algorithm_template,
    verify_template_contract_runtime,
)
from scripts.prepare_amq_template_subset import (
    acquire_output_lock,
    _candidate_requires_replan,
    _is_l0,
    _save_template_snapshot,
    _template_repair_needs_source,
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


def test_template_contract_requires_json_fixtures_for_every_dispatch_branch(
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
    rejected = validate_algorithm_template(project, require_contract_test=True)
    assert not rejected.passed
    assert any("mode='triple'" in error for error in rejected.errors)


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

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(template_adapter.subprocess, "run", fake_run)

    report = verify_template_contract_runtime(project)

    assert report.passed, report.to_json()
    docker_run = next(command for command in commands if command[:2] == ["docker", "run"])
    assert "--network" in docker_run and "none" in docker_run
    assert "--read-only" in docker_run
    assert "--cap-drop" in docker_run and "ALL" in docker_run
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

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal docker_runs
        if command[:2] == ["docker", "run"]:
            docker_runs += 1
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
    assert "源码读取工具已关闭" in local_only.system_prompt
    assert not _template_repair_needs_source(
        [
            "main_process 必须使用 Google 风格 docstring",
            "模板契约 fixture 过多",
        ]
    )
    assert _template_repair_needs_source(
        ["AttributeError: module has no attribute 'run'"]
    )


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
