"""Agent-assisted normalization of repositories to the IOEB algorithm template."""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
2. 只允许写根目录 main.py，以及在确有必要时写 requirements.txt。不得修改原算法源码。
3. main.py 必须提供顶层同步函数 main_process(...)；所有参数和返回值有类型注解，docstring 使用 Google 风格并包含 Args: 与 Returns:。
4. main_process 必须 import 并调用仓库中真实存在的算法能力；可以做输入校验、对象构造、数据转换和结果序列化，但不得复制/重写算法核心、返回伪结果或硬编码 benchmark 答案。
5. 模型加载、配置读取和资源解析必须在函数调用内部完成；禁止模块级 model = load_model() 或其他可变运行状态。
6. 面向调用者的输入输出应为 JSON 可表达的标量、list、dict；不得要求调用者访问容器内路径。若原算法确实需要文件，可接受 Base64/文本/结构化内容并在函数内部创建临时资源。
7. requirements.txt 只保留该入口运行所需的直接依赖，使用合法 PEP 508 规格；不得写本机绝对路径、git 凭证或不存在的版本。
8. 只能依据用户 wrap_intent 与仓库证据适配。你看不到、也不得猜测 benchmark task、ground truth 或验证脚本。
9. 写完 main.py 与 requirements.txt 后必须调用 verify_template；该调用会结束本轮，外层流程会把确定性错误反馈给下一轮修复。
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


def validate_algorithm_template(
    project_dir: str | Path, *, allow_explicit_unsupported: bool = False
) -> TemplateValidationReport:
    root = Path(project_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "mainFile": False,
        "mainProcess": False,
        "typedParameters": False,
        "typedReturn": False,
        "googleDocstring": False,
        "noModuleRuntimeState": False,
        "callsRepositoryCode": False,
        "requirementsFile": (root / "requirements.txt").is_file(),
        "readmeFile": (root / "README.md").is_file() or (root / "README.ioeb.md").is_file(),
        "explicitUnsupported": False,
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
    if not parameters:
        errors.append("main_process 至少需要一个业务输入参数")
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
    return TemplateValidationReport(not errors, errors, warnings, checks)


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
    description = "写入模板适配文件；只允许 main.py 与 requirements.txt。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "enum": ["main.py", "requirements.txt"]},
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
        if relative not in {"main.py", "requirements.txt"}:
            return ToolResult(error=f"不允许写入: {relative}")
        limit = 200_000 if relative == "main.py" else 50_000
        if not content.strip() or len(content) > limit or "\x00" in content:
            return ToolResult(error=f"{relative} 内容为空、含 NUL 或超过 {limit} 字符")
        (self.root / relative).write_text(content.rstrip() + "\n", encoding="utf-8")
        return ToolResult(output=f"已写入 {relative} ({len(content)} chars)")


class VerifyTemplate(Tool):
    name = "verify_template"
    description = "按 IOEB 提交模板确定性检查 main.py、main_process、注解、docstring、独立性和真实源码调用。"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, project_dir: str | Path) -> None:
        self.root = Path(project_dir).resolve()

    async def execute(self, **kwargs: Any) -> ToolResult:
        report = validate_algorithm_template(self.root)
        return ToolResult(output=report.to_json() if report.passed else "模板校验失败:\n" + report.to_json())


class BudgetedInspectRepository(InspectRepository):
    """Prevent repeated full-IR reads from consuming the adaptation budget."""

    def __init__(self, ir: RepositoryIR) -> None:
        super().__init__(ir, max_calls=1)


class BudgetedReadProjectFile(ReadProjectFile):
    """Bound source inspection while retaining the path-containment guarantees."""

    def __init__(self, project_dir: str | Path, *, max_reads: int = 12) -> None:
        super().__init__(project_dir, max_reads=max_reads)


def build_template_adapter_agent(project_dir: Path, ir: RepositoryIR) -> Agent:
    tools = ToolRegistry()
    tools.register(BudgetedInspectRepository(ir))
    tools.register(BudgetedReadProjectFile(project_dir))
    tools.register(WriteTemplateFile(project_dir))
    tools.register(VerifyTemplate(project_dir))
    tools.register(Terminate())
    return Agent(
        name="ioeb_template_adapter",
        llm=LLM(config.get_llm("reasoning")),
        tools=tools,
        system_prompt=TEMPLATE_ADAPTER_SYSTEM_PROMPT,
        max_steps=24,
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
        "只调用一次 inspect_repository，最多读取 12 个最相关文件，随后写 main.py 与 requirements.txt。"
        "不要读取或推断任何 benchmark 答案。完成后调用 verify_template；不要在校验后继续操作。\n"
        "仓库索引摘要：\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    )
