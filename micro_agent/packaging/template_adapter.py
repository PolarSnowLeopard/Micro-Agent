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
from packaging.utils import canonicalize_name

from micro_agent.core.agent import Agent
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.packaging.analyzer import RepositoryIR
from micro_agent.packaging.discovery import CapabilityDesign
from micro_agent.packaging.tools import (
    InspectRepository,
    ReadProjectFile,
    SearchProjectText,
)
from micro_agent.tool.base import Tool, ToolResult
from micro_agent.tool.registry import ToolRegistry


TEMPLATE_ADAPTER_SYSTEM_PROMPT = """你是 IOEB 算法仓库模板适配 Agent。你的任务是给现有仓库增加一个最薄的、真实可调用的模板入口，而不是重写算法或生成演示实现。

必须遵守：
1. 先且只调用一次 inspect_repository，再用 search_project_text 按 CapabilityDesign
   中的源码符号查找原仓库测试、示例、Notebook 与资产用法，然后阅读 README、依赖文件、
   相关入口和核心源码。每轮最多检索 5 次、读取 12 个文件；证据足够后立即写入口。
2. 只允许写根目录 main.py、在确有必要时写 requirements.txt，以及
   tests_ioeb/test_template_contract.py。不得修改原算法源码。
3. main.py 必须提供顶层同步函数 main_process(...)；所有参数和返回值有类型注解，docstring 使用 Google 风格并包含 Args: 与 Returns:。
4. main_process 必须 import 并调用仓库中真实存在的算法能力；可以做输入校验、对象构造、数据转换和结果序列化，但不得复制/重写算法核心、返回伪结果或硬编码 benchmark 答案。
   若源码或运行警告明确说明当前入口 deprecated 并给出替代类/函数，必须用
   search_project_text 定位并迁移到替代 API，不能只在注释中承认弃用后继续调用旧入口。
5. 模型加载、配置读取和资源解析必须在函数调用内部完成；禁止模块级 model = load_model() 或其他可变运行状态。
6. 面向调用者的输入输出应为 JSON 可表达的标量、list、dict；不得要求调用者访问容器内路径。若原算法确实需要文件，可接受 Base64/文本/结构化内容并在函数内部创建临时资源。
7. requirements.txt 只保留该入口运行所需的直接依赖，使用合法 PEP 508 规格；不得写本机绝对路径、git 凭证或不存在的版本。
8. 只能依据用户 wrap_intent 与仓库证据适配。你看不到、也不得猜测 benchmark task、ground truth 或验证脚本。
9. 必须生成 tests_ioeb/test_template_contract.py：直接从 main 导入 main_process。
   每个准备发布的独立能力只保留一个最小成功 fixture；不要为同一能力重复测试不同算法、
   配置值或错误边界。优先严格复用 CapabilityDesign.fixtureGuidance 指向的原仓库测试、
   示例、Notebook 或资产。领域输入确需计算时可在测试内动态构造，
   但传给 main_process 的最终参数必须完全 JSON 可序列化、不能含 NaN/Infinity，并会在隔离
   Docker 中捕获为服务 smoke；只能使用领域库提供的模拟器及显式固定种子，禁止 random、
   numpy.random、当前时间或手写随机波形。每个成功 fixture 至少用一个 assert 检查领域输出。
   不得只检查 callable、不得联网、不得启动子进程。
   可选的错误边界 fixture 必须放在 pytest.raises/相关 assertRaises 上下文中；它们不会成为
   服务 smoke 输入，也不能代替任何公开分支的成功 fixture。
10. 写完 main.py、契约测试与 requirements.txt 后必须调用 verify_template；该调用会结束本轮，
    外层流程会把确定性错误反馈给下一轮修复。
11. 禁止 eval、exec、compile 或其他动态执行用户文本的方式。将宽泛意图抽象为少量明确的
    operation 分支和 JSON 参数，每个分支必须直接调用已从仓库导入的真实函数/类；不得只把
    函数对象塞进映射后交给动态解释器。
12. 保持薄封装：main_process 最多 12 个显式参数、最多 8 个不同 operation，契约 fixture
    最多 30 个。通常选择 1–6 个与 wrap_intent 最相关、由仓库示例支持的内聚能力；同一能力
    不得提供多个成功 fixture，也不要机械暴露整个依赖库 API。
13. 成功时只返回领域结果；禁止返回 success/operation/result/error 控制信封。底层算法失败
    必须抛出带上下文的异常，不得在 except 中返回 success=false 或错误字符串伪装成功。
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
    allow_runtime_collected_contract: bool = False,
    max_contract_success_fixtures: int | None = None,
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
        "contractSuccessFixtureCount": 0,
        "contractUncollectedCallCount": 0,
        "contractStaticBindingCount": 0,
        "contractRuntimeCollectionRequired": False,
        "serverPathParameters": [],
        "noServerPathInterface": False,
        "controlEnvelopeReturnLines": [],
        "noControlEnvelopeReturns": False,
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

    server_path_parameters = _server_path_parameters(
        [parameter.arg for parameter in parameters],
        docstring,
    )
    checks["serverPathParameters"] = server_path_parameters
    if server_path_parameters:
        errors.append(
            "main_process 不得要求远程调用者提供容器内路径，相关参数: "
            + ", ".join(server_path_parameters)
            + "；请改为 Base64/文本/结构化内容并在函数内部创建临时资源。"
            "若仓库缺少完成真实算法调用所需的模型或数据资产，应明确拒绝适配，"
            "不能生成随机权重、伪 checkpoint 或只在测试中临时制造模型"
        )
    else:
        checks["noServerPathInterface"] = True

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
    control_envelope_lines = _control_envelope_return_lines(
        reachable_functions.values()
    )
    checks["controlEnvelopeReturnLines"] = control_envelope_lines
    if control_envelope_lines:
        errors.append(
            "main_process 及其辅助函数不得把成功/失败包装成 "
            "success/operation/result/error 控制信封后作为正常结果返回，相关行: "
            + ", ".join(map(str, control_envelope_lines))
            + "；成功只返回领域载荷，底层失败必须 raise 保留真实 traceback"
        )
    else:
        checks["noControlEnvelopeReturns"] = True
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
            allow_runtime_collected_contract=allow_runtime_collected_contract,
            max_contract_success_fixtures=max_contract_success_fixtures,
        )
        errors.extend(contract_errors)
        checks.update(contract_checks)
    return TemplateValidationReport(not errors, errors, warnings, checks)


_PATH_PARAMETER_TERMINALS = {
    "path",
    "paths",
    "dir",
    "dirs",
    "directory",
    "directories",
    "folder",
    "folders",
    "file",
    "files",
}
_CONTENT_PARAMETER_SUFFIXES = (
    "_base64",
    "_bytes",
    "_content",
    "_contents",
    "_csv",
    "_data",
    "_json",
    "_records",
    "_text",
    "_zip",
)
_MAX_CONTRACT_FIXTURE_BYTES = 2 * 1024 * 1024
_PATH_DESCRIPTION_MARKERS = (
    "file path",
    "paths to",
    "path to",
    "directory containing",
    "folder containing",
    "container path",
)


def _server_path_parameters(
    parameter_names: list[str],
    docstring: str,
) -> list[str]:
    """Find public parameters whose contract requires server-local paths."""

    doc_lines = docstring.lower().splitlines()
    result: list[str] = []
    for name in parameter_names:
        lowered = name.lower()
        if lowered.endswith(_CONTENT_PARAMETER_SUFFIXES):
            continue
        terminal = lowered.rsplit("_", 1)[-1]
        name_is_path = terminal in _PATH_PARAMETER_TERMINALS
        description_is_path = False
        for index, line in enumerate(doc_lines):
            stripped = line.strip()
            if not (
                stripped.startswith(f"{lowered}:")
                or stripped.startswith(f"{lowered} (")
            ):
                continue
            excerpt_parts = [stripped]
            for continuation in doc_lines[index + 1 :]:
                continuation_stripped = continuation.strip()
                if (
                    not continuation_stripped
                    or continuation_stripped in {"returns:", "raises:"}
                    or re.match(
                        r"^[a-z_][a-z0-9_]*(?:\s*\([^)]*\))?:",
                        continuation_stripped,
                    )
                ):
                    break
                excerpt_parts.append(continuation_stripped)
            excerpt = " ".join(excerpt_parts)
            description_is_path = any(
                marker in excerpt for marker in _PATH_DESCRIPTION_MARKERS
            )
            break
        if name_is_path or description_is_path:
            result.append(name)
    return result


def _control_envelope_return_lines(
    functions: Any,
) -> list[int]:
    """Find literal control envelopes that hide domain errors as success."""

    lines: set[int] = set()
    for function in functions:
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or not isinstance(
                node.value,
                ast.Dict,
            ):
                continue
            keys = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            control_payload_keys = keys & {"error", "operation", "result"}
            if (
                "success" in keys and bool(control_payload_keys)
            ) or (
                "error" in keys and bool(keys & {"operation", "result"})
            ):
                lines.add(node.lineno)
    return sorted(lines)


def _validate_template_contract_test(
    root: Path,
    main_function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    allow_runtime_collected_contract: bool = False,
    max_contract_success_fixtures: int | None = None,
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
        "contractSuccessFixtureCount": 0,
        "contractUncollectedCallCount": 0,
        "contractStaticBindingCount": 0,
        "contractRuntimeCollectionRequired": False,
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
    nondeterministic_calls = sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _is_nondeterministic_contract_call(node.func)
        }
    )
    checks["contractNondeterministicCallLines"] = nondeterministic_calls
    if nondeterministic_calls:
        errors.append(
            "模板契约测试必须可重复，禁止 random/numpy.random、当前时间、UUID "
            "等非确定性输入；请复用仓库 fixture/示例资产，或使用领域模拟器及显式"
            "固定种子。相关行: "
            + ", ".join(map(str, nondeterministic_calls))
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

    parent_by_node = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    executable_functions = _contract_executable_functions(tree)
    fixtures: list[dict[str, Any]] = []
    uncollected_calls = 0
    static_binding_count = 0
    dynamic_input_lines: list[int] = []
    executable_call_count = 0
    dynamic_call_outcomes: list[str] = []
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
        enclosing = _enclosing_contract_function(node, parent_by_node)
        if enclosing not in executable_functions:
            uncollected_calls += 1
            errors.append(
                "模板契约 main_process 调用不会被 pytest/unittest 收集执行："
                f"line={node.lineno}。不得把成功调用只放在未被测试使用的 "
                "@pytest.fixture 或普通 helper 中"
            )
            continue
        executable_call_count += 1
        fixture: dict[str, Any] = {}
        invalid_literal = False
        fixture_static_binding_count = 0
        if len(node.args) > len(parameter_names):
            errors.append(
                f"模板契约测试 main_process 调用参数过多: line={node.lineno}"
            )
            continue
        for name, value_node in zip(parameter_names, node.args):
            try:
                value, used_binding = _contract_json_literal(
                    value_node,
                    enclosing,
                )
                fixture[name] = value
                fixture_static_binding_count += int(used_binding)
            except (ValueError, SyntaxError):
                invalid_literal = True
        for keyword in node.keywords:
            if keyword.arg is None:
                invalid_literal = True
                continue
            try:
                value, used_binding = _contract_json_literal(
                    keyword.value,
                    enclosing,
                )
                fixture[keyword.arg] = value
                fixture_static_binding_count += int(used_binding)
            except (ValueError, SyntaxError):
                invalid_literal = True
        if invalid_literal:
            dynamic_input_lines.append(node.lineno)
            dynamic_call_outcomes.append(
                _contract_call_expected_outcome(
                    node,
                    parent_by_node,
                )
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
        static_binding_count += fixture_static_binding_count
        fixtures.append(
            {
                "line": node.lineno,
                "input": fixture,
                "expectedOutcome": _contract_call_expected_outcome(
                    node,
                    parent_by_node,
                ),
            }
        )
    checks["contractUncollectedCallCount"] = uncollected_calls
    checks["contractStaticBindingCount"] = static_binding_count
    checks["contractDynamicInputCallCount"] = len(dynamic_input_lines)
    checks["contractDynamicInputLines"] = sorted(dynamic_input_lines)
    checks["contractExecutableCallCount"] = executable_call_count
    checks["contractRuntimeCollectionRequired"] = bool(dynamic_input_lines)
    if (
        dynamic_input_lines
        and not fixtures
        and not allow_runtime_collected_contract
    ):
        errors.append(
            "模板契约测试的 main_process 输入必须是可审计的 JSON 字面量，"
            "至少一个成功 smoke 调用的完整输入必须能静态还原；允许直接字面量、"
            "同一测试函数内的纯 JSON 局部变量，以及由常量组成的 list/string "
            "拼接或重复表达式；不能仅依赖 fixture/helper/模块变量、函数调用、"
            "推导式或 **kwargs；"
            "相关调用行: "
            + ", ".join(map(str, sorted(dynamic_input_lines)))
        )

    if fixtures or (
        allow_runtime_collected_contract
        and executable_call_count
    ):
        checks["contractTestCallsMainProcess"] = True
        checks["contractFixtures"] = fixtures
    else:
        if allow_runtime_collected_contract:
            errors.append(
                "模板契约测试必须至少一次在 pytest/unittest 可收集测试中"
                "直接调用从 main 导入的 main_process"
            )
        else:
            errors.append(
                "模板契约测试必须至少一次直接调用从 main 导入的 main_process，"
                "并使用完整、可静态求值的 JSON 字面量输入"
            )
    success_fixtures = [
        fixture
        for fixture in fixtures
        if fixture["expectedOutcome"] == "success"
    ]
    checks["contractSuccessFixtureCount"] = len(success_fixtures)
    dynamic_success_count = sum(
        outcome == "success"
        for outcome in dynamic_call_outcomes
    )
    if (
        fixtures
        and not success_fixtures
        and not (
            allow_runtime_collected_contract
            and dynamic_success_count
        )
    ):
        errors.append(
            "模板契约测试只有预期失败输入；至少需要一个可作为服务 smoke 的成功 fixture"
        )
    if executable_call_count <= 30:
        checks["contractFixtureBudget"] = True
    else:
        errors.append(
            "模板契约 fixture 过多，薄封装最多允许 30 个；"
            f"当前 {executable_call_count} 个，请只保留与 wrap_intent 最相关的内聚能力"
        )

    assertion_count = sum(
        isinstance(node, ast.Assert)
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("assert")
            and len(node.func.attr) > len("assert")
            and node.func.attr
            not in {"assertRaises", "assertRaisesRegex"}
        )
        for node in ast.walk(tree)
    )
    error_fixture_count = (
        len(fixtures)
        - len(success_fixtures)
        + sum(outcome == "error" for outcome in dynamic_call_outcomes)
    )
    success_call_count = len(success_fixtures) + dynamic_success_count
    checks["contractExpectedSuccessCallCount"] = success_call_count
    if (
        max_contract_success_fixtures is not None
        and success_call_count > max_contract_success_fixtures
    ):
        errors.append(
            "每个已发现能力只允许一个最小成功 fixture；"
            f"当前 success_calls={success_call_count}, "
            f"capabilities={max_contract_success_fixtures}。"
            "删除同一能力的算法/配置/边界重复测试"
        )
    if (
        success_call_count
        and assertion_count + error_fixture_count >= executable_call_count
    ):
        checks["contractTestAssertions"] = True
    else:
        errors.append(
            "每个成功模板契约 fixture 至少需要一个 assert 验证领域输出；"
            f"当前 success_calls={success_call_count}, "
            f"asserts={assertion_count}"
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
            if fixture["expectedOutcome"] == "success"
            if selector in fixture["input"]
        }
        if serialized_values:
            operation_counts[selector] = len(serialized_values)
    checks["contractOperationCounts"] = operation_counts
    unbranched_selectors = sorted(
        selector
        for selector, count in operation_counts.items()
        if count > 1 and selector not in dispatch_cases
    )
    if unbranched_selectors:
        errors.append(
            "[contract_selector_semantics] 契约把以下字段当作多个公开操作，"
            "但 main_process 中没有对应的正向字面量分支: "
            + ", ".join(unbranched_selectors)
            + "；不得只校验后回显 selector。请让每个操作进入不同源码调用/结果路径，"
            "或删除这个伪操作参数"
        )
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
            for fixture in success_fixtures
            if selector in fixture["input"]
        }
        for value in values:
            if value not in observed:
                missing_cases.append(f"{selector}={value!r}")
    if missing_cases:
        checks["contractUncoveredBranches"] = missing_cases
        checks["contractBranchCoverage"] = False
    else:
        checks["contractBranchCoverage"] = True
    return errors, checks


def _is_nondeterministic_contract_call(function: ast.AST) -> bool:
    parts: list[str] = []
    current = function
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    parts.reverse()
    if not parts:
        return False
    if parts[0] in {"random", "secrets"} or "random" in parts[:-1]:
        return True
    terminal = parts[-1]
    if terminal in {
        "default_rng",
        "rand",
        "randint",
        "randn",
        "random_sample",
        "uuid1",
        "uuid4",
    }:
        return True
    return tuple(parts[-2:]) in {
        ("datetime", "now"),
        ("datetime", "today"),
        ("time", "time"),
    }


def _contract_json_literal(
    node: ast.AST,
    enclosing: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[Any, bool]:
    """Resolve a bounded JSON expression from the same collected test.

    Compact fixtures such as ``[0.0] * 1024`` are as auditable as spelling
    out every element. Resolve a deliberately small expression language
    without importing or executing any test code.
    """

    try:
        value = ast.literal_eval(node)
        _validate_contract_json_value(value)
        return value, False
    except (ValueError, SyntaxError):
        pass
    return _contract_json_expression(
        node,
        enclosing,
        resolving=frozenset(),
    )


def _contract_json_expression(
    node: ast.AST,
    enclosing: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    resolving: frozenset[str],
) -> tuple[Any, bool]:
    try:
        value = ast.literal_eval(node)
        _validate_contract_json_value(value)
        return value, False
    except (ValueError, SyntaxError):
        pass
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [
            _contract_json_expression(
                item,
                enclosing,
                resolving=resolving,
            )[0]
            for item in node.elts
        ]
        _validate_contract_json_value(values)
        return values, False
    if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
        keys = [
            _contract_json_expression(
                key,
                enclosing,
                resolving=resolving,
            )[0]
            for key in node.keys
        ]
        if not all(isinstance(key, str) for key in keys):
            raise ValueError("contract JSON object keys must be strings")
        values = [
            _contract_json_expression(
                value,
                enclosing,
                resolving=resolving,
            )[0]
            for value in node.values
        ]
        result = dict(zip(keys, values))
        _validate_contract_json_value(result)
        return result, False
    if isinstance(node, ast.UnaryOp) and isinstance(
        node.op,
        (ast.UAdd, ast.USub),
    ):
        operand, used_binding = _contract_json_expression(
            node.operand,
            enclosing,
            resolving=resolving,
        )
        if isinstance(operand, bool) or not isinstance(operand, (int, float)):
            raise ValueError("unary contract expression requires a number")
        result = operand if isinstance(node.op, ast.UAdd) else -operand
        _validate_contract_json_value(result)
        return result, used_binding
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
        left, left_bound = _contract_json_expression(
            node.left,
            enclosing,
            resolving=resolving,
        )
        right, right_bound = _contract_json_expression(
            node.right,
            enclosing,
            resolving=resolving,
        )
        if isinstance(node.op, ast.Add):
            if not (
                isinstance(left, type(right))
                and isinstance(left, (int, float, str, list))
                and not isinstance(left, bool)
            ):
                raise ValueError("unsupported contract addition")
            result = left + right
        else:
            if isinstance(left, bool) or isinstance(right, bool):
                raise ValueError("unsupported contract multiplication")
            if isinstance(left, (list, str)) and isinstance(right, int):
                result = left * right
            elif isinstance(right, (list, str)) and isinstance(left, int):
                result = right * left
            elif isinstance(left, (int, float)) and isinstance(
                right,
                (int, float),
            ):
                result = left * right
            else:
                raise ValueError("unsupported contract multiplication")
        _validate_contract_json_value(result)
        return result, left_bound or right_bound
    if not isinstance(node, ast.Name) or node.id in resolving:
        raise ValueError("not a contract JSON expression")

    candidate: ast.AST | None = None
    for statement in enclosing.body:
        if getattr(statement, "lineno", 0) >= getattr(node, "lineno", 0):
            break
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == node.id
                for target in statement.targets
            ):
                candidate = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == node.id
            and statement.value is not None
        ):
            candidate = statement.value
        elif (
            isinstance(statement, (ast.AugAssign, ast.For, ast.AsyncFor))
            and any(
                isinstance(target, ast.Name) and target.id == node.id
                for target in ast.walk(statement.target)
            )
        ):
            candidate = None
    if candidate is None:
        raise ValueError("unbound or dynamic contract input")
    value, _ = _contract_json_expression(
        candidate,
        enclosing,
        resolving=resolving | {node.id},
    )
    return value, True


def _validate_contract_json_value(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > _MAX_CONTRACT_FIXTURE_BYTES:
        raise ValueError("contract JSON expression exceeds 2 MiB")


def _contract_call_expected_outcome(
    call: ast.Call,
    parent_by_node: dict[ast.AST, ast.AST],
) -> str:
    current: ast.AST | None = call
    while current is not None:
        parent = parent_by_node.get(current)
        if isinstance(parent, (ast.With, ast.AsyncWith)):
            for item in parent.items:
                context = item.context_expr
                if (
                    isinstance(context, ast.Call)
                    and isinstance(context.func, ast.Attribute)
                    and context.func.attr in {
                        "raises",
                        "assertRaises",
                        "assertRaisesRegex",
                    }
                ):
                    return "error"
        if isinstance(parent, ast.Try) and current in parent.body and parent.handlers:
            return "error"
        current = parent
    return "success"


def _contract_executable_functions(
    tree: ast.Module,
) -> set[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return tests and pytest fixtures that a collected test actually uses."""

    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    tests = {node for node in functions if node.name.startswith("test_")}
    fixtures = {
        node.name: node
        for node in functions
        if any(_decorator_terminal_name(item) == "fixture" for item in node.decorator_list)
    }
    used_fixture_names: set[str] = set()
    for test in tests:
        used_fixture_names.update(
            parameter.arg
            for parameter in (
                *test.args.posonlyargs,
                *test.args.args,
                *test.args.kwonlyargs,
            )
            if parameter.arg in fixtures
        )
        used_fixture_names.update(_usefixtures_names(test.decorator_list))
    for name, fixture in fixtures.items():
        if any(
            _decorator_terminal_name(item) == "fixture"
            and isinstance(item, ast.Call)
            and any(
                keyword.arg == "autouse"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in item.keywords
            )
            for item in fixture.decorator_list
        ):
            used_fixture_names.add(name)

    pending = list(used_fixture_names)
    while pending:
        name = pending.pop()
        fixture = fixtures.get(name)
        if fixture is None:
            continue
        for parameter in (
            *fixture.args.posonlyargs,
            *fixture.args.args,
            *fixture.args.kwonlyargs,
        ):
            dependency = parameter.arg
            if dependency in fixtures and dependency not in used_fixture_names:
                used_fixture_names.add(dependency)
                pending.append(dependency)
    return tests | {
        fixture
        for name, fixture in fixtures.items()
        if name in used_fixture_names
    }


def _decorator_terminal_name(decorator: ast.expr) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _usefixtures_names(decorators: list[ast.expr]) -> set[str]:
    names: set[str] = set()
    for decorator in decorators:
        if (
            isinstance(decorator, ast.Call)
            and _decorator_terminal_name(decorator) == "usefixtures"
        ):
            for argument in decorator.args:
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value,
                    str,
                ):
                    names.add(argument.value)
    return names


def _enclosing_contract_function(
    node: ast.AST,
    parent_by_node: dict[ast.AST, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current = node
    while current in parent_by_node:
        current = parent_by_node[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
    return None


def template_contract_fixture_outcomes(
    path: str | Path,
) -> dict[int, str]:
    """Recover success/error labels for metadata written before labels existed."""

    contract_path = Path(path)
    try:
        tree = ast.parse(
            contract_path.read_text(encoding="utf-8", errors="replace"),
            filename=str(contract_path),
        )
    except (OSError, SyntaxError):
        return {}
    direct_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "main":
                    module_names.add(alias.asname or "main")
        elif isinstance(node, ast.ImportFrom) and node.module == "main":
            direct_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "main_process"
            )
    parent_by_node = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    executable_functions = _contract_executable_functions(tree)
    outcomes: dict[int, str] = {}
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
        if is_direct or is_module:
            outcomes[node.lineno] = (
                _contract_call_expected_outcome(
                    node,
                    parent_by_node,
                )
                if _enclosing_contract_function(
                    node,
                    parent_by_node,
                )
                in executable_functions
                else "uncollected"
            )
    return outcomes


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


_CONTRACT_FIXTURE_MARKER = "IOEB_TEMPLATE_CONTRACT_FIXTURES="
_CONTRACT_CAPTURE_RUNNER = r'''
import functools
import inspect
import json
import sys

import main
import pytest

_real_main_process = main.main_process
_records = []
_rejected = []
_truncated = False


def _caller_line():
    frame = inspect.currentframe()
    try:
        caller = frame.f_back.f_back if frame and frame.f_back else None
        return int(caller.f_lineno) if caller is not None else None
    finally:
        del frame


def _json_input(arguments):
    encoded = json.dumps(
        dict(arguments),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > 2097152:
        raise ValueError("captured input exceeds 2 MiB")
    return json.loads(encoded)


@functools.wraps(_real_main_process)
def _capturing_main_process(*args, **kwargs):
    global _truncated
    line = _caller_line()
    bound = inspect.signature(_real_main_process).bind(*args, **kwargs)
    bound.apply_defaults()
    result = _real_main_process(*args, **kwargs)
    try:
        captured = _json_input(bound.arguments)
    except (TypeError, ValueError) as exc:
        if len(_rejected) < 30:
            _rejected.append({"line": line, "reason": str(exc)})
        return result
    if len(_records) < 30:
        _records.append(
            {
                "line": line,
                "input": captured,
                "expectedOutcome": "success",
            }
        )
    else:
        _truncated = True
    return result


main.main_process = _capturing_main_process
try:
    _exit_code = pytest.main(
        [
            "-p",
            "no:cacheprovider",
            "-q",
            "tests_ioeb/test_template_contract.py",
        ]
    )
finally:
    print(
        "IOEB_TEMPLATE_CONTRACT_FIXTURES="
        + json.dumps(
            {
                "records": _records,
                "rejected": _rejected,
                "truncated": _truncated,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
sys.exit(int(_exit_code))
'''


def _parse_runtime_contract_capture(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if not line.startswith(_CONTRACT_FIXTURE_MARKER):
            continue
        try:
            payload = json.loads(line.removeprefix(_CONTRACT_FIXTURE_MARKER))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        records = payload.get("records")
        rejected = payload.get("rejected")
        if not isinstance(records, list) or not isinstance(rejected, list):
            return None
        return {
            "records": records,
            "rejected": rejected,
            "truncated": bool(payload.get("truncated")),
        }
    return None


def _without_runtime_contract_capture(stdout: str) -> str:
    omitted = 0
    retained: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(_CONTRACT_FIXTURE_MARKER):
            omitted += 1
        else:
            retained.append(line)
    if omitted:
        retained.append(
            f"[contract_fixture_capture] omitted {omitted} structured payload"
        )
    return "\n".join(retained)


def _apply_runtime_contract_capture(
    checks: dict[str, Any],
    static_report: TemplateValidationReport,
    completed: subprocess.CompletedProcess[str],
) -> str | None:
    capture = _parse_runtime_contract_capture(completed.stdout)
    static_success = [
        fixture
        for fixture in static_report.checks.get("contractFixtures", [])
        if isinstance(fixture, dict)
        and fixture.get("expectedOutcome") == "success"
    ]
    if capture is None:
        if static_report.checks.get("contractRuntimeCollectionRequired"):
            return (
                "[contract_fixture_capture] 契约测试包含动态输入，"
                "但隔离运行时未返回可解析的成功调用记录"
            )
        records = static_success
        rejected: list[Any] = []
        truncated = False
    else:
        records = [
            record
            for record in capture["records"]
            if isinstance(record, dict)
            and isinstance(record.get("input"), dict)
        ]
        rejected = capture["rejected"]
        truncated = capture["truncated"]
    checks["contractFixtures"] = records
    checks["contractSuccessFixtureCount"] = len(records)
    checks["contractRejectedFixtureInputs"] = rejected
    checks["contractFixtureCaptureTruncated"] = truncated
    if truncated:
        return "[contract_fixture_budget] 隔离运行时捕获到超过 30 个成功 fixture"
    if not records:
        detail = ""
        if rejected:
            detail = "；无法作为 JSON 服务输入的调用: " + json.dumps(
                rejected[:5],
                ensure_ascii=False,
            )
        return (
            "[contract_fixture_capture] 契约测试通过但没有捕获到"
            "可作为服务 smoke 的成功 JSON 输入"
            + detail
        )
    return None


def verify_template_contract_runtime(
    project_dir: str | Path,
    *,
    build_timeout: int = 900,
    runtime_timeout: int = 180,
    max_contract_success_fixtures: int | None = None,
) -> TemplateContractRuntimeReport:
    root = Path(project_dir).resolve()
    checks: dict[str, Any] = {
        "runtimeBackend": "docker",
        "networkDuringTest": False,
        "executionMode": "repository_source",
        "installedDistributionFallbackCandidates": [],
        "buildExitCode": None,
        "buildSeconds": None,
        "sourceTestExitCode": None,
        "installedDistributionTestExitCode": None,
        "installedDistributionTestSeconds": None,
        "testExitCode": None,
        "testSeconds": None,
        "functionalVerified": False,
        "contractFixtures": [],
        "contractSuccessFixtureCount": 0,
        "contractRejectedFixtureInputs": [],
        "contractFixtureCaptureTruncated": False,
    }
    errors: list[str] = []
    warnings: list[str] = []
    static_report = validate_algorithm_template(
        root,
        require_contract_test=True,
        allow_runtime_collected_contract=True,
        max_contract_success_fixtures=max_contract_success_fixtures,
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
    requirement_names = _runtime_requirement_names(root / "requirements.txt")
    evidence_modules = static_report.checks.get(
        "repositoryEvidenceModules",
        [],
    )
    fallback_candidates = sorted(
        module
        for module in evidence_modules
        if canonicalize_name(module) in requirement_names
    )
    checks["installedDistributionFallbackCandidates"] = fallback_candidates
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
        "# syntax=docker/dockerfile:1\n"
        "FROM python:3.11-slim-bookworm\n"
        "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 "
        "PIP_DISABLE_PIP_VERSION_CHECK=1 "
        "PYTHONPATH=/ioeb:/workspace:/workspace/src\n"
        "WORKDIR /workspace\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        "libexpat1 libgomp1 libgl1 libglib2.0-0 "
        "&& rm -rf /var/lib/apt/lists/*\n"
        "COPY requirements.txt /tmp/requirements.txt\n"
        "RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked "
        "python -m pip install --index-url "
        "\"${PIP_INDEX_URL}\" --timeout 120 --retries 5 "
        "\"pytest>=8,<9\" -r /tmp/requirements.txt\n"
        "COPY . /workspace\n"
        "RUN mkdir -p /ioeb && cp /workspace/main.py /ioeb/main.py "
        "&& cp -R /workspace/tests_ioeb /ioeb/tests_ioeb\n"
        "RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ioeb\n"
        "USER 10001:10001\n"
        "WORKDIR /ioeb\n"
        "CMD [\"python\", \"-m\", \"pytest\", \"-p\", \"no:cacheprovider\", \"-q\", "
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

        def runtime_command(
            python_path: str,
            workdir: str,
        ) -> list[str]:
            return [
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
                "--env",
                f"PYTHONPATH={python_path}",
                "--env",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "--env",
                "MPLCONFIGDIR=/tmp/matplotlib",
                "--env",
                "XDG_CACHE_HOME=/tmp/cache",
                "--workdir",
                workdir,
                "--entrypoint",
                "python",
                image,
                "-c",
                _CONTRACT_CAPTURE_RUNNER,
            ]

        try:
            runtime = subprocess.run(
                runtime_command(
                    "/workspace:/workspace/src:/ioeb",
                    "/workspace",
                ),
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
        checks["sourceTestExitCode"] = runtime.returncode
        checks["testExitCode"] = runtime.returncode
        if runtime.returncode == 0:
            capture_error = _apply_runtime_contract_capture(
                checks,
                static_report,
                runtime,
            )
            if capture_error:
                errors.append(capture_error)
            else:
                checks["functionalVerified"] = True
        elif fallback_candidates and _looks_like_local_distribution_shadow(
            runtime.stdout,
            runtime.stderr,
            fallback_candidates,
        ):
            fallback_started = time.perf_counter()
            try:
                fallback = subprocess.run(
                    runtime_command("/ioeb", "/ioeb"),
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
                    "[contract_distribution_fallback_timeout] "
                    f"同名发行包回退测试超过 {runtime_timeout} 秒"
                )
                return TemplateContractRuntimeReport(
                    False,
                    errors,
                    warnings,
                    checks,
                )
            checks["installedDistributionTestExitCode"] = fallback.returncode
            checks["testSeconds"] = round(
                time.perf_counter() - run_started,
                3,
            )
            checks["installedDistributionTestSeconds"] = round(
                time.perf_counter() - fallback_started,
                3,
            )
            checks["testExitCode"] = fallback.returncode
            if fallback.returncode == 0:
                capture_error = _apply_runtime_contract_capture(
                    checks,
                    static_report,
                    fallback,
                )
                if capture_error:
                    errors.append(capture_error)
                else:
                    checks["executionMode"] = "installed_distribution_fallback"
                    checks["functionalVerified"] = True
                    warnings.append(
                        "仓库源码包缺少可导入的编译产物；契约改用 requirements.txt "
                        "中的同名发行包验证，源码优先尝试及回退模式均已记录"
                    )
            else:
                errors.append(
                    "[contract_test] 仓库源码模式与同名发行包回退模式均失败。"
                    "\n源码模式:\n"
                    + _safe_process_tail(
                        _without_runtime_contract_capture(runtime.stdout),
                        runtime.stderr,
                    )
                    + "\n发行包回退模式:\n"
                    + _safe_process_tail(
                        _without_runtime_contract_capture(fallback.stdout),
                        fallback.stderr,
                    )
                )
        else:
            errors.append(
                "[contract_test] 无网络只读容器中的模板契约测试失败:\n"
                + _safe_process_tail(
                    _without_runtime_contract_capture(runtime.stdout),
                    runtime.stderr,
                )
            )
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


def _runtime_requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    if not path.is_file():
        return names
    for raw in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        if not requirement.url:
            names.add(canonicalize_name(requirement.name))
    return names


def _looks_like_local_distribution_shadow(
    stdout: str,
    stderr: str,
    candidates: list[str],
) -> bool:
    text = stdout + "\n" + stderr
    if "ImportError" not in text and "ModuleNotFoundError" not in text:
        return False
    if "/workspace/" not in text:
        return False
    lowered = text.lower()
    return any(candidate.lower() in lowered for candidate in candidates)


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


class ReadTemplateFile(Tool):
    """Read the current generated candidate without reopening repository search."""

    name = "read_template_file"
    description = (
        "读取当前生成候选的精确文本片段；只允许 main.py、requirements.txt 与"
        " tests_ioeb/test_template_contract.py。补丁 old 不匹配或校验给出行号时，"
        "先用此工具取得当前内容，再调用 patch_template_file。"
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
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        project_dir: str | Path,
        *,
        max_reads: int = 4,
        max_lines: int = 240,
    ) -> None:
        self.root = Path(project_dir).resolve()
        self.max_reads = max(1, max_reads)
        self.max_lines = max(1, max_lines)
        self._reads = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        relative = str(kwargs.get("path", ""))
        if relative not in {
            "main.py",
            "requirements.txt",
            "tests_ioeb/test_template_contract.py",
        }:
            return ToolResult(error=f"不允许读取: {relative}")
        if self._reads >= self.max_reads:
            return ToolResult(error=f"候选文件读取次数已达上限 {self.max_reads}")
        path = self.root / relative
        if not path.is_file() or path.is_symlink():
            return ToolResult(error=f"候选文件不存在或不可读取: {relative}")
        try:
            start = int(kwargs.get("start_line", 1))
            requested_end = kwargs.get("end_line")
            end = (
                int(requested_end)
                if requested_end is not None
                else start + self.max_lines - 1
            )
        except (TypeError, ValueError):
            return ToolResult(error="start_line 与 end_line 必须是整数")
        if start < 1 or end < start:
            return ToolResult(error="行号范围无效")
        if end - start + 1 > self.max_lines:
            return ToolResult(error=f"单次最多读取 {self.max_lines} 行")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        self._reads += 1
        if start > len(lines):
            return ToolResult(
                error=f"{relative} 只有 {len(lines)} 行，start_line={start} 超出范围"
            )
        actual_end = min(end, len(lines))
        content = "\n".join(lines[start - 1 : actual_end])
        return ToolResult(
            output=(
                f"# {relative} lines {start}-{actual_end} "
                f"(total {len(lines)})\n{content}"
            )
        )


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


class PatchTemplateFile(Tool):
    name = "patch_template_file"
    description = (
        "定向修复已有模板文件中的一处精确文本；old 必须在目标文件中恰好出现一次。"
        "适合修复单个 API、docstring、fixture 或断言，避免重写整个文件。"
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
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    }

    def __init__(self, project_dir: str | Path) -> None:
        self.root = Path(project_dir).resolve()

    async def execute(self, **kwargs: Any) -> ToolResult:
        relative = str(kwargs.get("path", ""))
        old = str(kwargs.get("old", ""))
        new = str(kwargs.get("new", ""))
        if relative not in {
            "main.py",
            "requirements.txt",
            "tests_ioeb/test_template_contract.py",
        }:
            return ToolResult(error=f"不允许修改: {relative}")
        if not old or old == new or "\x00" in old or "\x00" in new:
            return ToolResult(error="old 必须非空且与 new 不同，内容不能包含 NUL")
        path = self.root / relative
        if not path.is_file() or path.is_symlink():
            return ToolResult(error=f"目标文件不存在或不可修改: {relative}")
        content = path.read_text(encoding="utf-8", errors="replace")
        count = content.count(old)
        if count != 1:
            return ToolResult(
                error=(
                    f"old 在 {relative} 中必须恰好出现一次，当前 {count} 次。"
                    "请调用 read_template_file 读取校验错误附近的当前精确文本，"
                    "再用更小且唯一的 old 重试；不要猜测或重复相同补丁。"
                )
            )
        updated = content.replace(old, new, 1)
        limit = 200_000 if relative == "main.py" else 50_000
        if not updated.strip() or len(updated) > limit:
            return ToolResult(
                error=f"修改后 {relative} 为空或超过 {limit} 字符"
            )
        path.write_text(updated.rstrip() + "\n", encoding="utf-8")
        return ToolResult(
            output=f"已精确修改 {relative} ({len(old)} -> {len(new)} chars)"
        )


class VerifyTemplate(Tool):
    name = "verify_template"
    description = (
        "按 IOEB 提交模板确定性检查 main.py、main_process、注解、docstring、"
        "真实源码调用以及可执行契约测试的 JSON fixture、断言和能力覆盖。"
    )
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(
        self,
        project_dir: str | Path,
        *,
        max_contract_success_fixtures: int | None = None,
    ) -> None:
        self.root = Path(project_dir).resolve()
        self.max_contract_success_fixtures = max_contract_success_fixtures

    async def execute(self, **kwargs: Any) -> ToolResult:
        report = validate_algorithm_template(
            self.root,
            require_contract_test=True,
            allow_runtime_collected_contract=True,
            max_contract_success_fixtures=self.max_contract_success_fixtures,
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
    repair_source_reads: bool = True,
    capability_count: int | None = None,
) -> Agent:
    tools = ToolRegistry()
    if not repair:
        tools.register(BudgetedInspectRepository(ir))
    if not repair or repair_source_reads:
        tools.register(
            SearchProjectText(
                project_dir,
                max_calls=3 if repair else 5,
            )
        )
        tools.register(
            BudgetedReadProjectFile(
                project_dir,
                max_reads=4 if repair else 12,
            )
        )
    tools.register(WriteTemplateFile(project_dir))
    if repair:
        tools.register(ReadTemplateFile(project_dir))
        tools.register(PatchTemplateFile(project_dir))
    tools.register(
        VerifyTemplate(
            project_dir,
            max_contract_success_fixtures=capability_count,
        )
    )
    return Agent(
        name="ioeb_template_adapter",
        llm=LLM(config.get_llm("reasoning")),
        tools=tools,
        system_prompt=(
            TEMPLATE_ADAPTER_SYSTEM_PROMPT
            + (
                "\n这是已有模板候选的定向修复轮次。当前 main.py 和确定性错误已在请求中完整提供；"
                "禁止重新调用 inspect_repository。只读取错误指向的真实源码模块，保留候选中"
                "已通过的部分。若补丁 old 不匹配或错误带行号，先用 read_template_file 读取"
                "当前候选的精确局部，再用 patch_template_file 修改；仅在需要整体重构时才用"
                " write_template_file 覆盖错误涉及的 main.py、requirements.txt 或契约测试，"
                "随后调用 verify_template。"
                if repair
                else ""
            )
            + (
                "\n当前错误完全位于生成候选的结构、docstring 或契约 fixture；"
                "原仓库源码读取工具已关闭。可用 read_template_file 检查生成候选的精确局部，"
                "但不得尝试读取其他仓库文件。"
                if repair and not repair_source_reads
                else ""
            )
        ),
        max_steps=16 if repair else 24,
        max_observe=50_000,
        terminal_tools={"verify_template"},
        require_terminal_tool=True,
        no_tool_retry_limit=3,
        duplicate_tool_retry_limit=4,
        next_step_prompt=(
            "纯文本说明不算修复。必要时只读取一次候选精确局部；已有读取结果时"
            "禁止重读，必须立即调用 patch_template_file/write_template_file，"
            "完成后调用 verify_template。"
        ),
    )


def template_adapter_prompt(
    ir: RepositoryIR,
    wrap_intent: str,
    original_main: str | None,
    capability_design: CapabilityDesign | None = None,
) -> str:
    summary = {
        "fingerprint": ir.fingerprint,
        "fileCount": len(ir.files),
        "symbolCount": len(ir.symbols),
        "entrypointHints": ir.entrypointHints,
        "documentationFiles": list(ir.documentation),
        "testFiles": ir.testFiles[:30],
        "parseErrors": ir.parseErrors,
        "truncated": ir.truncated,
        "capabilityDiscovery": (
            capability_design.to_dict()
            if capability_design is not None
            else None
        ),
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
        + (
            "capabilityDiscovery 是前一阶段独立 Agent 已核对的能力边界与源码证据。"
            "以其中 1–6 个 capabilities 为模板入口的公开能力骨架；每个能力在 "
            "main_process 中使用明确的 operation 字面量分支，并从其 evidence 指向的"
            "原仓库测试/示例提取至少一个最小、直接、可静态求值的成功 fixture。"
            "若某候选因真实资产缺失无法执行，可删除该能力并在 README.ioeb.md 记录风险，"
            "但不能用伪数据或随机模型制造成功。\n"
            if capability_design is not None
            else ""
        )
        + "只调用一次 inspect_repository，最多读取 12 个最相关文件，随后写 main.py、"
        "tests_ioeb/test_template_contract.py 与必要的 requirements.txt。"
        "契约测试必须用 JSON 字面量调用 main_process 的每个公开分支并断言领域输出；"
        "长输入可先赋给同一测试函数内值为纯 JSON 字面量的局部变量，"
        "输入优先来自已读取的原仓库测试/doctest/示例。"
        "不要读取或推断任何 benchmark 答案。完成后调用 verify_template；不要在校验后继续操作。\n"
        "仓库索引摘要：\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    )
