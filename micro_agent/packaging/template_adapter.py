"""Agent-assisted normalization of repositories to the IOEB algorithm template."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from micro_agent.core.agent import Agent
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.packaging.analyzer import RepositoryIR
from micro_agent.packaging.tools import InspectRepository, ReadProjectFile
from micro_agent.tool.base import Tool, ToolResult
from micro_agent.tool.registry import ToolRegistry
from micro_agent.tool.terminate import Terminate


TEMPLATE_ADAPTER_SYSTEM_PROMPT = """你是 IOEB 算法仓库模板适配 Agent。你的任务是给现有仓库增加一个最薄的、真实可调用的模板入口，而不是重写算法或生成演示实现。

必须遵守：
1. 先且只调用一次 inspect_repository，再阅读 README、依赖文件、与用户封装意图相关的入口、测试和核心源码。每轮最多调用 12 次 read_project_file；证据足够后立即写入口，不得漫无目的遍历仓库。
2. 只允许写根目录 main.py、在确有必要时写 requirements.txt，以及
   tests_ioeb/test_template_contract.py。不得修改原算法源码。
3. main.py 必须提供顶层同步函数 main_process(...)；所有参数和返回值有类型注解，docstring 使用 Google 风格并包含 Args: 与 Returns:。
4. main_process 必须 import 并调用仓库中真实存在的算法能力；可以做输入校验、对象构造、数据转换和结果序列化，但不得复制/重写算法核心、返回伪结果或硬编码 benchmark 答案。
5. 模型加载、配置读取和资源解析必须在函数调用内部完成；禁止模块级 model = load_model() 或其他可变运行状态。
6. 面向调用者的输入输出应为 JSON 可表达的标量、list、dict；不得要求调用者访问容器内路径。若原算法确实需要文件，可接受 Base64/文本/结构化内容并在函数内部创建临时资源。
7. requirements.txt 只保留该入口运行所需的直接依赖，使用合法 PEP 508 规格；不得写本机绝对路径、git 凭证或不存在的版本。
8. 只能依据用户 wrap_intent 与仓库证据适配。你看不到、也不得猜测 benchmark task、ground truth 或验证脚本。
9. 必须生成 tests_ioeb/test_template_contract.py：直接从 main 导入 main_process，
   使用完整 JSON 字面量输入调用每个公开分支，每个 fixture 至少用一个 assert 检查领域输出。
   优先复用原仓库测试/doctest/示例中的输入，不得只检查 callable、不得联网、不得启动子进程。
10. 写完 main.py、契约测试与 requirements.txt 后必须调用 verify_template；该调用会结束本轮，
    外层流程会把确定性错误反馈给下一轮修复。
11. 禁止 eval、exec、compile 或其他动态执行用户文本的方式。将宽泛意图抽象为少量明确的
    operation 分支和 JSON 参数，每个分支必须直接调用已从仓库导入的真实函数/类；不得只把
    函数对象塞进映射后交给动态解释器。
12. 保持薄封装：main_process 最多 12 个显式参数、最多 8 个不同 operation，契约 fixture
    最多 30 个。通常选择 1–6 个与 wrap_intent 最相关、由仓库示例支持的内聚能力；可以为
    同一能力提供多个边界 fixture，但不要机械暴露整个依赖库 API。
"""


@dataclass(frozen=True)
class TemplateValidationReport:
    passed: bool
    errors: list[str]
    warnings: list[str]
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class TemplateContractRuntimeReport:
    passed: bool
    errors: list[str]
    warnings: list[str]
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def validate_algorithm_template(
    project_dir: str | Path,
    *,
    allow_explicit_unsupported: bool = False,
    require_contract_test: bool = False,
) -> TemplateValidationReport:
    root = Path(project_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "mainFile": False,
        "mainProcess": False,
        "interfaceParameterCount": 0,
        "typedParameters": False,
        "typedReturn": False,
        "googleDocstring": False,
        "noModuleRuntimeState": False,
        "noDynamicCodeExecution": False,
        "resolvableRepositoryImports": False,
        "callsRepositoryCode": False,
        "requirementsFile": (root / "requirements.txt").is_file(),
        "readmeFile": (root / "README.md").is_file() or (root / "README.ioeb.md").is_file(),
        "explicitUnsupported": False,
        "contractTestFile": False,
        "contractTestSyntax": False,
        "contractTestCallsMainProcess": False,
        "contractTestAssertions": False,
        "contractBranchCoverage": False,
        "contractFixtureBudget": False,
        "contractOperationCounts": {},
        "contractFixtures": [],
    }
    main_path = root / "main.py"
    if not main_path.is_file():
        errors.append("ZIP 项目根目录缺少 main.py")
        return TemplateValidationReport(False, errors, warnings, checks)
    checks["mainFile"] = True

    source = main_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename="main.py")
        compile(source, "main.py", "exec")
    except SyntaxError as exc:
        errors.append(f"main.py 语法错误: line={exc.lineno}, {exc.msg}")
        return TemplateValidationReport(False, errors, warnings, checks)

    invalid_imports = _invalid_local_import_members(root, tree)
    if invalid_imports:
        errors.append(
            "main.py 从仓库本地模块导入了不存在的成员: "
            + "; ".join(invalid_imports)
            + "；必须读取对应源码并使用真实公开 API，不能凭名称猜测"
        )
    else:
        checks["resolvableRepositoryImports"] = True

    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main_process"
    ]
    if len(functions) != 1:
        errors.append("main.py 必须且只能定义一个顶层 main_process 函数")
        return TemplateValidationReport(False, errors, warnings, checks)
    function = functions[0]
    checks["mainProcess"] = True
    if isinstance(function, ast.AsyncFunctionDef):
        errors.append("main_process 必须是可直接调用的同步函数")

    parameters = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    parameters = [parameter for parameter in parameters if parameter.arg not in {"self", "cls"}]
    checks["interfaceParameterCount"] = len(parameters)
    if not parameters:
        errors.append("main_process 至少需要一个业务输入参数")
    if len(parameters) > 12:
        errors.append(
            "main_process 显式参数过多，薄封装最多允许 12 个；"
            f"当前 {len(parameters)} 个，请收敛为少量内聚能力与结构化 JSON 参数"
        )
    missing_annotations = [parameter.arg for parameter in parameters if parameter.annotation is None]
    if function.args.vararg or function.args.kwarg:
        errors.append("main_process 不得使用 *args 或 **kwargs 隐藏接口契约")
    if missing_annotations:
        errors.append("main_process 参数缺少类型注解: " + ", ".join(missing_annotations))
    else:
        checks["typedParameters"] = True
    if function.returns is None:
        errors.append("main_process 缺少返回值类型注解")
    else:
        checks["typedReturn"] = True

    docstring = ast.get_docstring(function) or ""
    if "Args:" not in docstring or "Returns:" not in docstring:
        errors.append("main_process 必须使用包含 Args: 与 Returns: 的 Google 风格 docstring")
    else:
        checks["googleDocstring"] = True
        for parameter in parameters:
            if f"{parameter.arg}:" not in docstring and f"{parameter.arg} (" not in docstring:
                errors.append(f"main_process docstring 未说明参数 {parameter.arg}")

    if _contains_forbidden_pass(function) or any(
        isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(function)
    ):
        errors.append("main_process 不得包含 pass/yield 占位或流式返回")
    if any(
        (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and node.value.value is Ellipsis
        )
        or (
            isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return))
            and isinstance(getattr(node, "value", None), ast.Constant)
            and getattr(node, "value").value is Ellipsis
        )
        for node in ast.walk(function)
    ):
        errors.append("main_process 不得使用省略号代替实现")
    runtime_assignments: list[int] = []
    safe_module_calls = {"len", "min", "max", "sum", "tuple", "frozenset"}
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            value = node.value
        if value is not None:
            calls = [child for child in ast.walk(value) if isinstance(child, ast.Call)]
            if any(not _is_safe_module_call(call, safe_module_calls) for call in calls):
                runtime_assignments.append(getattr(node, "lineno", 0))
    if runtime_assignments:
        errors.append(
            "禁止模块级调用初始化运行状态，相关行: "
            + ", ".join(str(line) for line in runtime_assignments)
        )
    else:
        checks["noModuleRuntimeState"] = True

    local_import_roots: set[str] = set()
    for candidate in root.iterdir():
        if candidate.name.startswith(".") or candidate.name == "main.py":
            continue
        if candidate.is_file() and candidate.suffix == ".py":
            local_import_roots.add(candidate.stem)
        elif candidate.is_dir() and (
            (candidate / "__init__.py").is_file() or any(candidate.glob("*.py"))
        ):
            local_import_roots.add(candidate.name)
    src_dir = root / "src"
    if src_dir.is_dir():
        for candidate in src_dir.iterdir():
            if candidate.is_file() and candidate.suffix == ".py":
                local_import_roots.add(candidate.stem)
            elif candidate.is_dir() and (
                (candidate / "__init__.py").is_file() or any(candidate.glob("*.py"))
            ):
                local_import_roots.add(candidate.name)

    for package_marker in root.rglob("__init__.py"):
        if not any(part in {".git", ".venv", "venv", "__pycache__"} for part in package_marker.parts):
            local_import_roots.add(package_marker.parent.name)

    repository_import_roots = _repository_import_roots(root)

    imported_names: set[str] = set()
    local_imported_names: set[str] = set()
    evidence_imported_names: set[str] = set()
    imported_modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".")[0]
                module_root = alias.name.split(".")[0]
                imported_names.add(bound_name)
                imported_modules[bound_name] = module_root
                if module_root in local_import_roots:
                    local_imported_names.add(bound_name)
                elif module_root in repository_import_roots:
                    evidence_imported_names.add(bound_name)
        elif isinstance(node, ast.ImportFrom):
            module_root = (node.module or "").split(".")[0]
            for alias in node.names:
                bound_name = alias.asname or alias.name
                imported_names.add(bound_name)
                imported_modules[bound_name] = module_root
                if node.level or module_root in local_import_roots:
                    local_imported_names.add(bound_name)
                elif module_root in repository_import_roots:
                    evidence_imported_names.add(bound_name)
    top_level_functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reachable_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {
        "main_process": function
    }
    pending = [function]
    call_roots: set[str] = set()
    while pending:
        current = pending.pop()
        for node in ast.walk(current):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            while isinstance(target, ast.Attribute):
                target = target.value
            if not isinstance(target, ast.Name):
                continue
            call_roots.add(target.id)
            helper = top_level_functions.get(target.id)
            if helper is not None and target.id not in reachable_functions:
                reachable_functions[target.id] = helper
                pending.append(helper)
    reachable_names = {
        node.id
        for reachable in reachable_functions.values()
        for node in ast.walk(reachable)
        if isinstance(node, ast.Name)
    }
    dynamic_execution = sorted(
        {
            node.func.id
            for reachable in reachable_functions.values()
            for node in ast.walk(reachable)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"compile", "eval", "exec"}
        }
    )
    if dynamic_execution:
        errors.append(
            "main_process 及其辅助函数禁止动态执行用户文本: "
            + ", ".join(dynamic_execution)
            + "；请改为显式 operation 分支并直接调用仓库 API"
        )
    else:
        checks["noDynamicCodeExecution"] = True
    indirect_imports: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        bound_globals: set[str] = set()
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bound_globals.update(
                    child.id for child in ast.walk(target) if isinstance(child, ast.Name)
                )
        elif isinstance(node.target, ast.Name):
            bound_globals.add(node.target.id)
        if not (bound_globals & reachable_names) or node.value is None:
            continue
        indirect_imports.update(
            child.id
            for child in ast.walk(node.value)
            if isinstance(child, ast.Name) and child.id in imported_names
        )

    local_repository_calls = sorted(local_imported_names & (call_roots | indirect_imports))
    external_evidence_calls = sorted(evidence_imported_names & (call_roots | indirect_imports))
    repository_calls = [*local_repository_calls, *external_evidence_calls]

    raises = [node for node in ast.walk(function) if isinstance(node, ast.Raise)]
    explicit_unsupported = bool(raises) and not repository_calls
    checks["explicitUnsupported"] = explicit_unsupported
    if repository_calls:
        checks["callsRepositoryCode"] = True
        checks["repositoryCallRoots"] = repository_calls
        checks["reachableLocalFunctions"] = sorted(reachable_functions)
        checks["localImportRoots"] = sorted(local_import_roots)
        checks["repositoryEvidenceImportRoots"] = sorted(repository_import_roots)
        checks["repositoryEvidenceMode"] = (
            "local_module_call" if local_repository_calls else "source_declared_dependency_call"
        )
        checks["repositoryEvidenceModules"] = sorted(
            {imported_modules[name] for name in repository_calls if imported_modules.get(name)}
        )
        if external_evidence_calls and not local_repository_calls:
            warnings.append(
                "入口复用了原仓库源码/Notebook 已声明的外部算法依赖；"
                "该证据模式将在派生集元数据中单独记录"
            )
    elif allow_explicit_unsupported and explicit_unsupported:
        warnings.append("L0 负样本仅提供显式 unsupported 入口，不代表算法能力可封装")
    else:
        errors.append("main_process 未调用任何从原仓库导入的真实算法能力")

    lowered = source.lower()
    forbidden = [marker for marker in ("placeholder", "mock result", "fake result", "hardcoded") if marker in lowered]
    if forbidden and not allow_explicit_unsupported:
        errors.append("main.py 含禁止的演示/伪实现标记: " + ", ".join(forbidden))

    if not checks["requirementsFile"]:
        errors.append("ZIP 项目根目录缺少 requirements.txt")
    if not checks["readmeFile"]:
        errors.append("ZIP 项目缺少 README.md 或 README.ioeb.md")
    if require_contract_test and not allow_explicit_unsupported:
        contract_errors, contract_checks = _validate_template_contract_test(
            root,
            function,
        )
        errors.extend(contract_errors)
        checks.update(contract_checks)
    return TemplateValidationReport(not errors, errors, warnings, checks)


def _validate_template_contract_test(
    root: Path,
    main_function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    checks: dict[str, Any] = {
        "contractTestFile": False,
        "contractTestSyntax": False,
        "contractTestCallsMainProcess": False,
        "contractTestAssertions": False,
        "contractBranchCoverage": False,
        "contractFixtureBudget": False,
        "contractOperationCounts": {},
        "contractFixtures": [],
    }
    relative = Path("tests_ioeb") / "test_template_contract.py"
    path = root / relative
    if not path.is_file() or path.is_symlink():
        return (
            [
                "模板缺少 tests_ioeb/test_template_contract.py；"
                "必须提供从 main 导入 main_process 的端到端可执行契约测试"
            ],
            checks,
        )
    checks["contractTestFile"] = True
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=relative.as_posix())
        compile(source, relative.as_posix(), "exec")
    except SyntaxError as exc:
        errors.append(
            "tests_ioeb/test_template_contract.py 语法错误: "
            f"line={exc.lineno}, {exc.msg}"
        )
        return errors, checks
    checks["contractTestSyntax"] = True

    forbidden_imports = {
        "httpx",
        "openai",
        "requests",
        "socket",
        "subprocess",
        "urllib",
    }
    imported_forbidden: set[str] = set()
    direct_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = alias.name.split(".")[0]
                if root_name in forbidden_imports:
                    imported_forbidden.add(root_name)
                if alias.name == "main":
                    module_names.add(alias.asname or "main")
        elif isinstance(node, ast.ImportFrom):
            root_name = (node.module or "").split(".")[0]
            if root_name in forbidden_imports:
                imported_forbidden.add(root_name)
            if node.module == "main":
                direct_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "main_process"
                )
    if imported_forbidden:
        errors.append(
            "模板契约测试不得访问网络或启动子进程，禁止导入: "
            + ", ".join(sorted(imported_forbidden))
        )

    parameters = [
        *main_function.args.posonlyargs,
        *main_function.args.args,
        *main_function.args.kwonlyargs,
    ]
    parameter_names = [parameter.arg for parameter in parameters]
    positional_count = len(main_function.args.posonlyargs) + len(
        main_function.args.args
    )
    positional_required = positional_count - len(main_function.args.defaults)
    required = set(parameter_names[:positional_required])
    required.update(
        parameter.arg
        for parameter, default in zip(
            main_function.args.kwonlyargs,
            main_function.args.kw_defaults,
        )
        if default is None
    )

    fixtures: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_direct = isinstance(node.func, ast.Name) and node.func.id in direct_names
        is_module = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "main_process"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_names
        )
        if not (is_direct or is_module):
            continue
        fixture: dict[str, Any] = {}
        invalid_literal = False
        if len(node.args) > len(parameter_names):
            errors.append(
                f"模板契约测试 main_process 调用参数过多: line={node.lineno}"
            )
            continue
        for name, value_node in zip(parameter_names, node.args):
            try:
                fixture[name] = ast.literal_eval(value_node)
            except (ValueError, SyntaxError):
                invalid_literal = True
        for keyword in node.keywords:
            if keyword.arg is None:
                invalid_literal = True
                continue
            try:
                fixture[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, SyntaxError):
                invalid_literal = True
        if invalid_literal:
            errors.append(
                "模板契约测试的 main_process 输入必须是可审计的 JSON 字面量，"
                f"不能引用运行时变量或使用 **kwargs: line={node.lineno}"
            )
            continue
        unknown = sorted(set(fixture) - set(parameter_names))
        missing = sorted(required - set(fixture))
        if unknown:
            errors.append(
                f"模板契约测试 main_process 调用包含未知参数 {unknown}: line={node.lineno}"
            )
            continue
        if missing:
            errors.append(
                f"模板契约测试 main_process 调用缺少必填参数 {missing}: line={node.lineno}"
            )
            continue
        try:
            json.dumps(fixture, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            errors.append(
                "模板契约测试输入必须完全 JSON 可序列化且不能包含 NaN/Infinity: "
                f"line={node.lineno}"
            )
            continue
        fixtures.append(
            {
                "line": node.lineno,
                "input": fixture,
            }
        )

    if fixtures:
        checks["contractTestCallsMainProcess"] = True
        checks["contractFixtures"] = fixtures
    else:
        errors.append(
            "模板契约测试必须至少一次直接调用从 main 导入的 main_process，"
            "并使用完整 JSON 字面量输入"
        )
    if len(fixtures) <= 30:
        checks["contractFixtureBudget"] = True
    else:
        errors.append(
            "模板契约 fixture 过多，薄封装最多允许 30 个；"
            f"当前 {len(fixtures)} 个，请只保留与 wrap_intent 最相关的内聚能力"
        )

    assertion_count = sum(
        isinstance(node, ast.Assert)
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("assert")
            and len(node.func.attr) > len("assert")
        )
        for node in ast.walk(tree)
    )
    if fixtures and assertion_count >= len(fixtures):
        checks["contractTestAssertions"] = True
    else:
        errors.append(
            "每个模板契约 fixture 至少需要一个 assert 验证领域输出；"
            f"当前 calls={len(fixtures)}, asserts={assertion_count}"
        )

    dispatch_cases = _literal_dispatch_cases(main_function)
    selector_candidates = set(dispatch_cases)
    selector_candidates.update(
        name
        for name in parameter_names
        if name in {"action", "capability", "mode", "operation"}
    )
    operation_counts: dict[str, int] = {}
    for selector in sorted(selector_candidates):
        serialized_values = {
            json.dumps(
                fixture["input"][selector],
                ensure_ascii=False,
                sort_keys=True,
            )
            for fixture in fixtures
            if selector in fixture["input"]
        }
        if serialized_values:
            operation_counts[selector] = len(serialized_values)
    checks["contractOperationCounts"] = operation_counts
    overbroad_selectors = {
        selector: count
        for selector, count in operation_counts.items()
        if count > 8
    }
    if overbroad_selectors:
        errors.append(
            "模板接口 operation 过多，薄封装最多允许 8 个不同操作: "
            + ", ".join(
                f"{selector}={count}"
                for selector, count in overbroad_selectors.items()
            )
        )
    missing_cases: list[str] = []
    for selector, values in dispatch_cases.items():
        observed = {
            fixture["input"].get(selector)
            for fixture in fixtures
            if selector in fixture["input"]
        }
        for value in values:
            if value not in observed:
                missing_cases.append(f"{selector}={value!r}")
    if missing_cases:
        errors.append(
            "模板契约测试未覆盖 main_process 的全部字面量分支: "
            + ", ".join(missing_cases)
        )
    else:
        checks["contractBranchCoverage"] = True
    return errors, checks


def _literal_dispatch_cases(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, list[Any]]:
    parameter_names = {
        parameter.arg
        for parameter in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    result: dict[str, list[Any]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        selector = node.left.id
        if selector not in parameter_names or len(node.ops) != len(node.comparators):
            continue
        values: list[Any] = []
        for operator, comparator in zip(node.ops, node.comparators):
            if isinstance(operator, ast.Eq):
                try:
                    value = ast.literal_eval(comparator)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    values.append(value)
            elif isinstance(operator, ast.In) and isinstance(
                comparator,
                (ast.Tuple, ast.List, ast.Set),
            ):
                for item in comparator.elts:
                    try:
                        value = ast.literal_eval(item)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        values.append(value)
        for value in values:
            bucket = result.setdefault(selector, [])
            if value not in bucket:
                bucket.append(value)
    return result


def verify_template_contract_runtime(
    project_dir: str | Path,
    *,
    build_timeout: int = 900,
    runtime_timeout: int = 180,
) -> TemplateContractRuntimeReport:
    root = Path(project_dir).resolve()
    checks: dict[str, Any] = {
        "runtimeBackend": "docker",
        "networkDuringTest": False,
        "buildExitCode": None,
        "buildSeconds": None,
        "testExitCode": None,
        "testSeconds": None,
        "functionalVerified": False,
    }
    errors: list[str] = []
    warnings: list[str] = []
    static_report = validate_algorithm_template(
        root,
        require_contract_test=True,
    )
    if not static_report.passed:
        return TemplateContractRuntimeReport(
            False,
            ["隔离运行前模板静态门禁未通过: " + "; ".join(static_report.errors)],
            warnings,
            checks,
        )
    requirement_errors = _runtime_requirement_errors(root / "requirements.txt")
    if requirement_errors:
        return TemplateContractRuntimeReport(
            False,
            requirement_errors,
            warnings,
            checks,
        )
    try:
        docker = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        docker = None
    if docker is None or docker.returncode != 0:
        return TemplateContractRuntimeReport(
            False,
            ["[runtime_backend_unavailable] Docker daemon unavailable"],
            warnings,
            checks,
        )

    run_id = uuid.uuid4().hex[:12]
    image = f"ioeb-template-contract:{run_id}"
    dockerfile = root / f".ioeb-template-contract-{run_id}.Dockerfile"
    dockerignore = root / f"{dockerfile.name}.dockerignore"
    dockerfile.write_text(
        "FROM python:3.11-slim-bookworm\n"
        "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 "
        "PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/app:/app/src\n"
        "WORKDIR /app\n"
        "COPY requirements.txt /tmp/requirements.txt\n"
        "RUN python -m pip install --no-cache-dir --index-url "
        "\"${PIP_INDEX_URL}\" --timeout 120 --retries 5 "
        "\"pytest>=8,<9\" -r /tmp/requirements.txt\n"
        "COPY . /app\n"
        "RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ioeb\n"
        "USER 10001:10001\n"
        "CMD [\"python\", \"-m\", \"pytest\", \"-q\", "
        "\"tests_ioeb/test_template_contract.py\"]\n",
        encoding="utf-8",
    )
    dockerignore.write_text(
        ".git\n.hg\n.svn\n.venv\nvenv\n__pycache__\n.pytest_cache\n"
        ".mypy_cache\n.ruff_cache\nnode_modules\ndist\nbuild\n",
        encoding="utf-8",
    )
    try:
        build_started = time.perf_counter()
        try:
            build = subprocess.run(
                [
                    "docker",
                    "build",
                    "--file",
                    dockerfile.name,
                    "--tag",
                    image,
                    ".",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=build_timeout,
                env={**os.environ, "DOCKER_BUILDKIT": "1"},
            )
        except subprocess.TimeoutExpired:
            checks["buildSeconds"] = round(
                time.perf_counter() - build_started,
                3,
            )
            errors.append(
                f"[contract_build_timeout] 模板契约镜像构建超过 {build_timeout} 秒"
            )
            return TemplateContractRuntimeReport(
                False,
                errors,
                warnings,
                checks,
            )
        checks["buildSeconds"] = round(time.perf_counter() - build_started, 3)
        checks["buildExitCode"] = build.returncode
        if build.returncode != 0:
            errors.append(
                "[contract_build] 模板契约镜像构建失败:\n"
                + _safe_process_tail(build.stdout, build.stderr)
            )
            return TemplateContractRuntimeReport(
                False,
                errors,
                warnings,
                checks,
            )

        run_started = time.perf_counter()
        try:
            runtime = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=512m",
                    "--tmpfs",
                    "/home/ioeb:rw,noexec,nosuid,size=64m",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "256",
                    "--memory",
                    "4g",
                    "--cpus",
                    "2",
                    image,
                ],
                capture_output=True,
                text=True,
                timeout=runtime_timeout,
            )
        except subprocess.TimeoutExpired:
            checks["testSeconds"] = round(
                time.perf_counter() - run_started,
                3,
            )
            errors.append(
                f"[contract_test_timeout] 模板契约测试超过 {runtime_timeout} 秒"
            )
            return TemplateContractRuntimeReport(
                False,
                errors,
                warnings,
                checks,
            )
        checks["testSeconds"] = round(time.perf_counter() - run_started, 3)
        checks["testExitCode"] = runtime.returncode
        if runtime.returncode != 0:
            errors.append(
                "[contract_test] 无网络只读容器中的模板契约测试失败:\n"
                + _safe_process_tail(runtime.stdout, runtime.stderr)
            )
        else:
            checks["functionalVerified"] = True
    finally:
        dockerfile.unlink(missing_ok=True)
        dockerignore.unlink(missing_ok=True)
        try:
            cleanup = subprocess.run(
                ["docker", "image", "rm", "--force", image],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            cleanup = None
        if (
            (cleanup is None or cleanup.returncode != 0)
            and checks["buildExitCode"] == 0
        ):
            warnings.append("模板契约运行镜像未能自动清理")
    return TemplateContractRuntimeReport(
        not errors and bool(checks["functionalVerified"]),
        errors,
        warnings,
        checks,
    )


def _runtime_requirement_errors(path: Path) -> list[str]:
    if not path.is_file():
        return ["[contract_requirements] 根目录缺少 requirements.txt"]
    errors: list[str] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            errors.append(
                "[contract_requirements] requirements.txt 禁止 pip 命令行选项或递归引用: "
                f"line={line_number}"
            )
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            errors.append(
                "[contract_requirements] 非法 PEP 508 依赖: "
                f"line={line_number}, value={line[:120]!r}"
            )
            continue
        if requirement.url:
            errors.append(
                "[contract_requirements] 契约运行禁止 URL/VCS/本地路径依赖: "
                f"line={line_number}, package={requirement.name}"
            )
    return errors


def _safe_process_tail(stdout: str, stderr: str, *, limit: int = 4_000) -> str:
    text = (stdout + "\n" + stderr).strip()
    text = re.sub(
        r"(?i)(https?://)([^/@\s]+)@",
        r"\1<redacted>@",
        text,
    )
    return text[-limit:] if text else "<no output>"


def _invalid_local_import_members(root: Path, tree: ast.Module) -> list[str]:
    invalid: list[str] = []
    search_roots = [root]
    if (root / "src").is_dir():
        search_roots.append(root / "src")
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        parts = node.module.split(".")
        module_path: Path | None = None
        is_package = False
        for search_root in search_roots:
            file_candidate = search_root.joinpath(*parts).with_suffix(".py")
            package_candidate = search_root.joinpath(*parts, "__init__.py")
            if file_candidate.is_file():
                module_path = file_candidate
                break
            if package_candidate.is_file():
                module_path = package_candidate
                is_package = True
                break
        if module_path is None:
            continue
        try:
            module_tree = ast.parse(
                module_path.read_text(encoding="utf-8", errors="replace"),
                filename=str(module_path),
            )
        except (OSError, SyntaxError):
            continue
        available = _module_bound_names(module_tree)
        has_open_exports = any(
            (
                isinstance(child, ast.ImportFrom)
                and any(alias.name == "*" for alias in child.names)
            )
            or (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "__getattr__"
            )
            for child in module_tree.body
        )
        package_dir = module_path.parent if is_package else None
        for alias in node.names:
            if alias.name == "*" or alias.name in available or has_open_exports:
                continue
            if package_dir is not None and (
                (package_dir / f"{alias.name}.py").is_file()
                or (package_dir / alias.name / "__init__.py").is_file()
                or any(package_dir.glob(f"{alias.name}.*"))
            ):
                continue
            invalid.append(f"{node.module}.{alias.name} (main.py:{node.lineno})")
    return sorted(set(invalid))


def _module_bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    class ModuleBindingVisitor(ast.NodeVisitor):
        """Collect bindings created while executing a module.

        Conditional and guarded imports/assignments still create legitimate
        module attributes. Function and class bodies have their own namespace,
        however, and comprehension targets do not leak into the module.
        """

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            names.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store):
                names.add(node.id)

        def visit_Import(self, node: ast.Import) -> None:
            names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name != "*"
            )

        def visit_ListComp(self, node: ast.ListComp) -> None:
            return

        def visit_SetComp(self, node: ast.SetComp) -> None:
            return

        def visit_DictComp(self, node: ast.DictComp) -> None:
            return

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            return

    ModuleBindingVisitor().visit(tree)
    return names


def _contains_forbidden_pass(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    def visit(node: ast.AST, *, inside_except: bool = False) -> bool:
        if isinstance(node, ast.Pass):
            return not inside_except
        for child in ast.iter_child_nodes(node):
            if visit(child, inside_except=inside_except or isinstance(child, ast.ExceptHandler)):
                return True
        return False

    return visit(function)


def _is_safe_module_call(call: ast.Call, safe_names: set[str]) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id in safe_names
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return (call.func.value.id, call.func.attr) in {("logging", "getLogger")}
    return False


def _repository_import_roots(root: Path) -> set[str]:
    imports: set[str] = set()
    ignored_parts = {".git", ".venv", "venv", "__pycache__", "node_modules"}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored_parts for part in path.parts):
            continue
        if path.name in {"main.py", "template_adaptation.json"}:
            continue
        sources: list[str] = []
        try:
            if path.suffix == ".py" and path.stat().st_size <= 1_000_000:
                sources.append(path.read_text(encoding="utf-8", errors="replace"))
            elif path.suffix == ".ipynb" and path.stat().st_size <= 5_000_000:
                notebook = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                sources.extend(
                    "".join(cell.get("source", []))
                    if isinstance(cell.get("source"), list)
                    else str(cell.get("source", ""))
                    for cell in notebook.get("cells", [])
                    if cell.get("cell_type") == "code"
                )
        except (OSError, ValueError, TypeError):
            continue
        for source in sources:
            try:
                parsed = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(parsed):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
    return {name for name in imports if name and name not in sys.stdlib_module_names}


class WriteTemplateFile(Tool):
    name = "write_template_file"
    description = (
        "写入模板适配文件；只允许 main.py、requirements.txt 与"
        " tests_ioeb/test_template_contract.py。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "enum": [
                    "main.py",
                    "requirements.txt",
                    "tests_ioeb/test_template_contract.py",
                ],
            },
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, project_dir: str | Path) -> None:
        self.root = Path(project_dir).resolve()

    async def execute(self, **kwargs: Any) -> ToolResult:
        relative = str(kwargs.get("path", ""))
        content = str(kwargs.get("content", ""))
        allowed = {
            "main.py",
            "requirements.txt",
            "tests_ioeb/test_template_contract.py",
        }
        if relative not in allowed:
            return ToolResult(error=f"不允许写入: {relative}")
        limit = 200_000 if relative == "main.py" else 50_000
        if not content.strip() or len(content) > limit or "\x00" in content:
            return ToolResult(error=f"{relative} 内容为空、含 NUL 或超过 {limit} 字符")
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        return ToolResult(output=f"已写入 {relative} ({len(content)} chars)")


class VerifyTemplate(Tool):
    name = "verify_template"
    description = (
        "按 IOEB 提交模板确定性检查 main.py、main_process、注解、docstring、"
        "真实源码调用以及可执行契约测试的 JSON fixture、断言和分支覆盖。"
    )
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, project_dir: str | Path) -> None:
        self.root = Path(project_dir).resolve()

    async def execute(self, **kwargs: Any) -> ToolResult:
        report = validate_algorithm_template(
            self.root,
            require_contract_test=True,
        )
        return ToolResult(output=report.to_json() if report.passed else "模板校验失败:\n" + report.to_json())


class BudgetedInspectRepository(InspectRepository):
    """Prevent repeated full-IR reads from consuming the adaptation budget."""

    def __init__(self, ir: RepositoryIR) -> None:
        super().__init__(ir, max_calls=1)


class BudgetedReadProjectFile(ReadProjectFile):
    """Bound source inspection while retaining the path-containment guarantees."""

    def __init__(self, project_dir: str | Path, *, max_reads: int = 12) -> None:
        super().__init__(project_dir, max_reads=max_reads)


def build_template_adapter_agent(
    project_dir: Path,
    ir: RepositoryIR,
    *,
    repair: bool = False,
) -> Agent:
    tools = ToolRegistry()
    if not repair:
        tools.register(BudgetedInspectRepository(ir))
    tools.register(BudgetedReadProjectFile(project_dir, max_reads=4 if repair else 12))
    tools.register(WriteTemplateFile(project_dir))
    tools.register(VerifyTemplate(project_dir))
    tools.register(Terminate())
    return Agent(
        name="ioeb_template_adapter",
        llm=LLM(config.get_llm("reasoning")),
        tools=tools,
        system_prompt=(
            TEMPLATE_ADAPTER_SYSTEM_PROMPT
            + (
                "\n这是已有模板候选的定向修复轮次。当前 main.py 和确定性错误已在请求中完整提供；"
                "禁止重新调用 inspect_repository。只读取错误指向的真实源码模块，保留候选中"
                "已通过的部分，只覆盖写入错误涉及的 main.py、requirements.txt 或契约测试，"
                "随后调用 verify_template。"
                if repair
                else ""
            )
        ),
        max_steps=16 if repair else 24,
        max_observe=50_000,
        terminal_tools={"verify_template", "terminate"},
    )


def template_adapter_prompt(ir: RepositoryIR, wrap_intent: str, original_main: str | None) -> str:
    summary = {
        "fingerprint": ir.fingerprint,
        "fileCount": len(ir.files),
        "symbolCount": len(ir.symbols),
        "entrypointHints": ir.entrypointHints,
        "documentationFiles": list(ir.documentation),
        "testFiles": ir.testFiles[:30],
        "parseErrors": ir.parseErrors,
        "truncated": ir.truncated,
    }
    original_note = (
        f"原仓库已有 main.py，已在适配副本中保留为 {original_main}；需要时从该文件导入。"
        if original_main
        else "原仓库根目录没有 main.py。"
    )
    return (
        "请把当前仓库适配为 IOEB ZIP 算法模板。\n"
        f"wrap_intent（唯一业务需求来源）：{wrap_intent}\n"
        f"{original_note}\n"
        "只调用一次 inspect_repository，最多读取 12 个最相关文件，随后写 main.py、"
        "tests_ioeb/test_template_contract.py 与必要的 requirements.txt。"
        "契约测试必须用 JSON 字面量直接调用 main_process 的每个公开分支并断言领域输出，"
        "输入优先来自已读取的原仓库测试/doctest/示例。"
        "不要读取或推断任何 benchmark 答案。完成后调用 verify_template；不要在校验后继续操作。\n"
        "仓库索引摘要：\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    )
