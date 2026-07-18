"""Deterministic acceptance gate for Agent-generated MCP artifacts."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from micro_agent.packaging.analyzer import RepositoryAnalyzer, RepositoryIR
from micro_agent.packaging.dependency_inspector import unresolved_import_dependencies
from micro_agent.packaging.models import PackagingPlan, PlanValidationError


REQUIRED_FILES = {
    "server.py", "adapters.py", "requirements.txt", "Dockerfile",
    "docker-compose.yml", "packaging_plan.json", "ioeb-service.json",
    "algorithm_loader.py", "runtime_guardrails.py", "system-packages.txt",
    "requirements-cpu.txt",
}
SYSTEM_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?$")


@dataclass
class VerificationReport:
    passed: bool
    checks: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class ArtifactVerifier:
    def __init__(self, artifact_dir: str | Path, expected_plan: PackagingPlan) -> None:
        self.root = Path(artifact_dir).resolve()
        self.expected_plan = expected_plan

    def verify(self) -> VerificationReport:
        report = VerificationReport(passed=False)
        ready_marker = self.root / ".ioeb-ready"
        ready_marker.unlink(missing_ok=True)

        if not self.root.is_dir():
            report.errors.append("产物目录不存在")
            return report

        self._check_paths(report)
        self._check_required_files(report)
        actual_plan = self._check_plan(report)
        self._check_python(report, actual_plan or self.expected_plan)
        self._check_adapter_source_boundary(report)
        self._check_dependencies(report)
        self._check_import_dependencies(report, actual_plan or self.expected_plan)
        self._check_system_packages(report)
        self._check_container_files(report)
        self._check_manifest(report)
        self._check_source_copy(report)
        report.checks["smokeTestsPlanned"] = sum(
            1 for tool in self.expected_plan.tools if tool.get("smokeTest", {}).get("enabled")
        )
        if report.checks["smokeTestsPlanned"] == 0:
            report.warnings.append("Agent 未找到可在容器内自动执行的 smoke test 输入")
        report.passed = not report.errors
        return report

    def _check_paths(self, report: VerificationReport) -> None:
        symlinks = [str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_symlink()]
        report.checks["symlinkCount"] = len(symlinks)
        if symlinks:
            report.errors.append(f"产物中不允许符号链接: {', '.join(symlinks[:5])}")

    def _check_required_files(self, report: VerificationReport) -> None:
        missing = sorted(name for name in REQUIRED_FILES if not (self.root / name).is_file())
        report.checks["requiredFiles"] = sorted(REQUIRED_FILES)
        if missing:
            report.errors.append(f"缺少必要文件: {', '.join(missing)}")

    def _check_plan(self, report: VerificationReport) -> PackagingPlan | None:
        path = self.root / "packaging_plan.json"
        if not path.is_file():
            return None
        try:
            plan = PackagingPlan.validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, PlanValidationError) as exc:
            report.errors.append(f"packaging_plan.json 无效: {exc}")
            return None
        report.checks["plannedTools"] = plan.tool_names
        if plan.tool_names != self.expected_plan.tool_names:
            report.errors.append(
                "生成阶段擅自改变了已审核工具集合: "
                f"expected={self.expected_plan.tool_names}, actual={plan.tool_names}"
            )
        return plan

    def _check_python(self, report: VerificationReport, plan: PackagingPlan) -> None:
        trees: dict[str, ast.Module] = {}
        for name in ("server.py", "adapters.py"):
            path = self.root / name
            if not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8")
                trees[name] = ast.parse(source, filename=name)
                compile(source, name, "exec")
            except (OSError, SyntaxError) as exc:
                report.errors.append(f"{name} 语法无效: {exc}")
        if "server.py" not in trees:
            return

        decorated = _mcp_tool_functions(trees["server.py"])
        report.checks["registeredTools"] = sorted(decorated)
        planned = set(plan.tool_names)
        actual = set(decorated)
        if planned - actual:
            report.errors.append(f"缺少 MCP Tool 注册: {', '.join(sorted(planned - actual))}")
        if actual - planned:
            report.errors.append(f"存在规划外 MCP Tool: {', '.join(sorted(actual - planned))}")

        tool_by_name = {tool["name"]: tool for tool in plan.tools}
        for name, function in decorated.items():
            if name not in tool_by_name:
                continue
            if not (ast.get_docstring(function) or "").strip():
                report.errors.append(f"MCP Tool {name} 缺少 docstring")
            expected_args = set(tool_by_name[name]["inputSchema"].get("properties", {}).keys())
            actual_args = {
                arg.arg
                for arg in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
                if arg.arg not in {"self", "ctx", "context"}
            }
            if function.args.vararg or function.args.kwarg:
                report.errors.append(f"MCP Tool {name} 不允许 *args/**kwargs，必须暴露明确 JSON Schema")
            if expected_args != actual_args:
                report.errors.append(
                    f"MCP Tool {name} 参数与规划不一致: expected={sorted(expected_args)}, actual={sorted(actual_args)}"
                )
            for arg in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]:
                if arg.arg not in {"self", "ctx", "context"} and arg.annotation is None:
                    report.errors.append(f"MCP Tool {name} 参数 {arg.arg} 缺少类型注解")
            if function.returns is None:
                report.errors.append(f"MCP Tool {name} 缺少返回类型注解")

        adapter_functions = _top_level_functions(trees.get("adapters.py"))
        report.checks["adapterFunctions"] = sorted(name for name in adapter_functions if name in planned)
        missing_adapters = planned - set(adapter_functions)
        if missing_adapters:
            report.errors.append(f"缺少同名语义适配函数: {', '.join(sorted(missing_adapters))}")
        for name in planned & set(adapter_functions):
            function = adapter_functions[name]
            expected_args = set(tool_by_name[name]["inputSchema"].get("properties", {}).keys())
            actual_args = {
                arg.arg
                for arg in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
                if arg.arg != "self"
            }
            if expected_args != actual_args or function.args.vararg or function.args.kwarg:
                report.errors.append(
                    f"适配函数 {name} 参数与规划不一致: expected={sorted(expected_args)}, actual={sorted(actual_args)}"
                )
            if _returns_from_except(function):
                report.errors.append(
                    f"适配函数 {name} 在 except 中返回普通结果，会把执行失败伪装成成功；必须抛出异常"
                )

        self._check_source_failure_sentinels(
            report,
            adapter_functions,
            tool_by_name,
            trees.get("adapters.py"),
        )

        if "adapters.py" in trees:
            self._check_algorithm_imports(report, trees["adapters.py"], plan)
            imported_bindings = _imported_bound_names(trees["adapters.py"])
            shadowed = sorted(planned & imported_bindings & set(adapter_functions))
            if shadowed:
                report.errors.append(
                    "适配函数覆盖了同名源码导入并会递归调用: " + ", ".join(shadowed)
                    + "；源码导入必须使用 alias"
                )

        server_text = (self.root / "server.py").read_text(encoding="utf-8")
        for required_import in (
            "from mcp.server.fastmcp import FastMCP",
        ):
            if required_import not in server_text:
                report.errors.append(f"server.py 缺少协议基线导入: {required_import}")
        if "mcp = FastMCP(" not in server_text or "mcp.sse_app(" not in server_text:
            report.errors.append("server.py 未通过 FastMCP.sse_app() 初始化版本兼容的 SSE transport")
        if '"/sse"' not in server_text and "'/sse'" not in server_text:
            report.errors.append("server.py 未暴露 /sse 端点")
        if '"/messages/"' not in server_text and "'/messages/'" not in server_text:
            report.errors.append("server.py 未挂载 /messages/ 端点")

    def _check_dependencies(self, report: VerificationReport) -> None:
        path = self.root / "requirements.txt"
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        missing = [name for name in ("mcp", "starlette", "uvicorn") if name not in text]
        if missing:
            report.errors.append(f"requirements.txt 缺少运行依赖: {', '.join(missing)}")

    def _check_import_dependencies(
        self,
        report: VerificationReport,
        plan: PackagingPlan,
    ) -> None:
        modules = {
            symbol.rsplit(".", 1)[0]
            for tool in plan.tools
            for symbol in tool.get("sourceSymbols", [])
        }
        unresolved = unresolved_import_dependencies(
            self.root / "algorithm",
            source_modules=modules,
            adapter_path=self.root / "adapters.py",
            requirement_paths=(
                self.root / "requirements.txt",
                self.root / "requirements-cpu.txt",
            ),
        )
        report.checks["unresolvedImportDependencies"] = unresolved
        if unresolved:
            details = [
                f"{name} -> {item['distribution']} ({', '.join(item['files'][:3])})"
                for name, item in unresolved.items()
            ]
            report.errors.append(
                "受工具源码导入链使用、但依赖清单未声明的第三方模块: "
                + "; ".join(details)
            )

    def _check_system_packages(self, report: VerificationReport) -> None:
        path = self.root / "system-packages.txt"
        if not path.is_file():
            return
        packages = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        invalid = [package for package in packages if not SYSTEM_PACKAGE_RE.fullmatch(package)]
        report.checks["systemPackages"] = packages
        if invalid:
            report.errors.append(
                "system-packages.txt 只允许 Debian 包名，禁止参数、命令和 URL: "
                + ", ".join(invalid[:10])
            )

    def _check_source_failure_sentinels(
        self,
        report: VerificationReport,
        adapter_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
        tool_by_name: dict[str, dict[str, Any]],
        adapter_tree: ast.Module | None,
    ) -> None:
        algorithm_root = self.root / "algorithm"
        if not algorithm_root.is_dir():
            return
        try:
            ir = RepositoryAnalyzer().analyze(algorithm_root)
        except (OSError, ValueError):
            return
        all_sentinels = {
            symbol.qualifiedName: symbol.failureReturns
            for symbol in ir.symbols
            if symbol.failureReturns
        }
        report.checks["sourceFailureSentinels"] = all_sentinels
        for tool_name, tool in tool_by_name.items():
            risky = _reachable_failure_sentinels(
                ir,
                set(tool.get("sourceSymbols", [])),
                all_sentinels,
            )
            function = adapter_functions.get(tool_name)
            source_names = {
                symbol.rsplit(".", 1)[-1]
                for symbol in tool.get("sourceSymbols", [])
            }
            risky_names = _import_aliases_for_names(
                adapter_tree,
                source_names | {symbol.rsplit(".", 1)[-1] for symbol in risky},
            )
            if (
                risky
                and function is not None
                and not _guards_failure_return_from_calls(
                    function,
                    risky_names,
                    adapter_functions=adapter_functions,
                )
            ):
                report.errors.append(
                    f"适配函数 {tool_name} 调用的源码会把异常作为普通字符串返回，"
                    "必须检查该源码调用的返回值并 raise，不能让 MCP 把失败标成成功: "
                    + json.dumps(risky, ensure_ascii=False)
                )

    def _check_adapter_source_boundary(self, report: VerificationReport) -> None:
        path = self.root / "adapters.py"
        if not path.is_file():
            return
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return
        unsafe_assets = _asset_literals_without_algorithm_root(tree, self.root / "algorithm")
        if unsafe_assets:
            report.errors.append(
                "模型/资源路径未基于 algorithm_loader.ALGORITHM_DIR: "
                + ", ".join(sorted(unsafe_assets))
            )
        if _has_runtime_guardrail_fallback(tree):
            report.errors.append(
                "runtime_guardrails 是只读必备模块，不允许捕获 ImportError 后提供占位/降级实现"
            )
        sys_path_lines = _sys_path_mutation_lines(tree)
        if sys_path_lines:
            report.errors.append(
                "adapters.py 不允许修改 sys.path；algorithm_loader 已按低优先级接入算法目录，"
                "自行插入会让提交仓库中的同名目录遮蔽已安装依赖。违规行: "
                + ", ".join(str(line) for line in sys_path_lines)
            )
        zip_inputs = _zip_input_names(self.expected_plan)
        for tool_name, input_names in zip_inputs.items():
            function = _top_level_functions(tree).get(tool_name)
            if function is None:
                continue
            guarded = _direct_guarded_names(function)
            missing = sorted(input_names - guarded)
            if missing:
                report.errors.append(
                    f"适配函数 {tool_name} 必须将 Base64 ZIP 参数直接传给 "
                    f"decode_safe_zip: {', '.join(missing)}"
                )
            double_decoded = sorted(input_names & _base64_decoded_names(function))
            if double_decoded:
                report.errors.append(
                    f"适配函数 {tool_name} 对 ZIP 参数重复 Base64 解码: "
                    + ", ".join(double_decoded)
                )

    def _check_algorithm_imports(
        self,
        report: VerificationReport,
        tree: ast.Module,
        plan: PackagingPlan,
    ) -> None:
        source_roots = _algorithm_module_roots(self.root / "algorithm")
        entry_roots = {
            symbol.split(".", 1)[0]
            for tool in plan.tools
            for symbol in tool.get("sourceSymbols", [])
            if isinstance(symbol, str) and symbol
        }
        loader_index: int | None = None
        entry_import_line: int | None = None
        legacy_imports: list[tuple[int, str]] = []
        top_level_import_ids: set[int] = set()
        for index, node in enumerate(tree.body):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            top_level_import_ids.add(id(node))
            modules = _imported_modules(node)
            if any(module == "algorithm_loader" for module in modules):
                loader_index = index if loader_index is None else min(loader_index, index)
            for module in modules:
                root = module.split(".", 1)[0]
                if root in source_roots and root != "algorithm":
                    legacy_imports.append((index, module))
                if root in entry_roots:
                    entry_import_line = (
                        node.lineno
                        if entry_import_line is None
                        else min(entry_import_line, node.lineno)
                    )
        nested_legacy = {
            module
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in top_level_import_ids
            for module in _imported_modules(node)
            if module.split(".", 1)[0] in source_roots
            and module.split(".", 1)[0] != "algorithm"
            and loader_index is None
        }
        invalid = sorted(
            {module
            for index, module in legacy_imports
            if loader_index is None or loader_index >= index} | nested_legacy
        )
        if invalid:
            report.errors.append(
                "源码模块导入前必须先 `from algorithm_loader import ALGORITHM_DIR`，"
                "确保原仓库及其旧式绝对导入从 algorithm/ 解析: " + ", ".join(invalid)
            )
        late_shims = _late_guarded_compatibility_shims(tree, entry_import_line)
        if late_shims:
            report.errors.append(
                "第三方兼容映射必须在源码入口模块导入前生效；当前先导入了源码，"
                "随后才为缺失属性安装 shim，无法修复 import-time 失败。请移动这些映射，"
                "再导入 main/predictor/api 等源码模块。违规行: "
                + ", ".join(str(line) for line in late_shims)
            )

    def _check_container_files(self, report: VerificationReport) -> None:
        dockerfile = self.root / "Dockerfile"
        if dockerfile.is_file():
            text = dockerfile.read_text(encoding="utf-8", errors="replace")
            if "CMD" not in text or "server.py" not in text:
                report.errors.append("Dockerfile 未以 server.py 作为启动入口")
            if "COPY ." not in text and "COPY --chown=10001:10001 . " not in text:
                report.errors.append("Dockerfile 未复制完整产物")
            if "system-packages.txt" not in text or "apt-get install" not in text:
                report.errors.append("Dockerfile 未接入受控系统依赖清单")
            if "requirements-cpu.txt" not in text or "PYTORCH_CPU_INDEX_URL" not in text:
                report.errors.append("Dockerfile 未接入标准容器的 CPU 推理依赖清单")
            if "USER 10001:10001" not in text:
                report.errors.append("Dockerfile 未使用固定非 root 运行用户")
        compose = self.root / "docker-compose.yml"
        if compose.is_file():
            text = compose.read_text(encoding="utf-8", errors="replace")
            if "build:" not in text:
                report.errors.append("docker-compose.yml 缺少 build 配置")
            if "volumes:" in text:
                report.errors.append("部署产物不允许声明宿主机 volumes")

    def _check_manifest(self, report: VerificationReport) -> None:
        path = self.root / "ioeb-service.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.errors.append(f"ioeb-service.json 无效: {exc}")
            return
        if data.get("engine") != "agentic":
            report.errors.append("ioeb-service.json 未声明 agentic 引擎")
        if data.get("endpoint") != "/sse" or data.get("port") != 8000:
            report.errors.append("ioeb-service.json 的传输端点配置不兼容当前平台")

    def _check_source_copy(self, report: VerificationReport) -> None:
        root = self.root / "algorithm"
        python_files = list(root.rglob("*.py")) if root.is_dir() else []
        report.checks["algorithmPythonFiles"] = len(python_files)
        if not python_files:
            report.errors.append("algorithm/ 中没有保留提交的 Python 源码")


def _mcp_tool_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    result: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_mcp_tool_decorator(decorator) for decorator in node.decorator_list):
            result[node.name] = node
    return result


def _sys_path_mutation_lines(tree: ast.Module) -> list[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            if (
                node.func.attr in {"append", "extend", "insert", "remove", "pop", "clear"}
                and isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "sys"
                and target.attr == "path"
            ):
                lines.add(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "sys"
                and target.attr == "path"
                for target in targets
            ):
                lines.add(node.lineno)
    return sorted(lines)


def _top_level_functions(
    tree: ast.Module | None,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    if tree is None:
        return {}
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _returns_from_except(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.ExceptHandler) and any(
            isinstance(child, ast.Return) for statement in node.body for child in ast.walk(statement)
        ):
            return True
    return False


def _guards_failure_return_from_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    risky_call_names: set[str],
    *,
    adapter_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    visited: set[str] | None = None,
) -> bool:
    """Require a branch that raises based on a risky source call's result.

    A generic input-validation ``raise`` is insufficient: the adapter must bind
    the return value of the source callable and use that exact binding in the
    condition which converts a failure sentinel into an exception.
    """
    risky_results: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        call_name = _call_name(value.func)
        if not call_name or call_name.rsplit(".", 1)[-1] not in risky_call_names:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        risky_results.update(
            child.id
            for target in targets
            for child in ast.walk(target)
            if isinstance(child, ast.Name)
        )

    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        tested_names = {
            child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)
        }
        raises = any(
            isinstance(child, ast.Raise)
            for statement in node.body
            for child in ast.walk(statement)
        )
        if raises and tested_names & risky_results:
            return True
    if adapter_functions:
        if _guards_tainted_parameters(
            function,
            risky_results,
            adapter_functions=adapter_functions,
            visited=visited,
        ):
            return True
        seen = set(visited or ())
        if function.name in seen:
            return False
        seen.add(function.name)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node.func)
            if not call_name:
                continue
            helper = adapter_functions.get(call_name.rsplit(".", 1)[-1])
            if helper is None or helper.name in seen:
                continue
            positional_parameters = [
                *helper.args.posonlyargs,
                *helper.args.args,
            ]
            guarded_helper_parameters = {
                parameter.arg
                for argument, parameter in zip(node.args, positional_parameters)
                if isinstance(argument, ast.Name) and argument.id in risky_results
            }
            guarded_helper_parameters.update(
                keyword.arg
                for keyword in node.keywords
                if keyword.arg
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id in risky_results
            )
            if guarded_helper_parameters and _raises_from_tested_parameters(
                helper, guarded_helper_parameters
            ):
                return True
            if _guards_failure_return_from_calls(
                helper,
                risky_call_names,
                adapter_functions=adapter_functions,
                visited=seen,
            ):
                return True
    return False


def _guards_tainted_parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    tainted_names: set[str],
    *,
    adapter_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    visited: set[str] | None = None,
) -> bool:
    if not tainted_names:
        return False
    if _raises_from_tested_parameters(function, tainted_names):
        return True
    seen = set(visited or ())
    if function.name in seen:
        return False
    seen.add(function.name)
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func)
        if not call_name:
            continue
        helper = adapter_functions.get(call_name.rsplit(".", 1)[-1])
        if helper is None or helper.name in seen:
            continue
        positional_parameters = [
            *helper.args.posonlyargs,
            *helper.args.args,
        ]
        mapped = {
            parameter.arg
            for argument, parameter in zip(node.args, positional_parameters)
            if any(
                isinstance(child, ast.Name) and child.id in tainted_names
                for child in ast.walk(argument)
            )
        }
        parameter_by_name = {
            parameter.arg: parameter
            for parameter in [*positional_parameters, *helper.args.kwonlyargs]
        }
        mapped.update(
            keyword.arg
            for keyword in node.keywords
            if keyword.arg in parameter_by_name
            and any(
                isinstance(child, ast.Name) and child.id in tainted_names
                for child in ast.walk(keyword.value)
            )
        )
        if mapped and _guards_tainted_parameters(
            helper,
            mapped,
            adapter_functions=adapter_functions,
            visited=seen,
        ):
            return True
    return False


def _raises_from_tested_parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter_names: set[str],
) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        tested_names = {
            child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)
        }
        if not tested_names & parameter_names:
            continue
        if any(
            isinstance(child, ast.Raise)
            for statement in node.body
            for child in ast.walk(statement)
        ):
            return True
    return False


def _import_aliases_for_names(tree: ast.Module | None, names: set[str]) -> set[str]:
    result = set(names)
    if tree is None:
        return result
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name in names:
                result.add(alias.asname or alias.name)
    return result


def _reachable_failure_sentinels(
    ir: RepositoryIR,
    roots: set[str],
    sentinels: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Find failure-returning source symbols reachable from planned roots."""
    by_name = {symbol.qualifiedName: symbol for symbol in ir.symbols}
    by_tail: dict[str, set[str]] = {}
    for qualified in by_name:
        by_tail.setdefault(qualified.rsplit(".", 1)[-1], set()).add(qualified)
    visited: set[str] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        symbol = by_name.get(current)
        if symbol is None:
            continue
        for call in symbol.calls:
            exact = {call} if call in by_name else set()
            candidates = exact or by_tail.get(call.rsplit(".", 1)[-1], set())
            pending.extend(candidates - visited)
    return {symbol: sentinels[symbol] for symbol in visited if symbol in sentinels}


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _imported_bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return names


def _zip_input_names(plan: PackagingPlan) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for tool in plan.tools:
        properties = tool.get("inputSchema", {}).get("properties", {})
        if not isinstance(properties, dict):
            continue
        for name, schema in properties.items():
            description = str(schema.get("description", "")) if isinstance(schema, dict) else ""
            combined = f"{name} {description}".lower()
            if "zip" in combined and ("base64" in combined or "file" in combined):
                result.setdefault(tool["name"], set()).add(name)
    return result


def _algorithm_module_roots(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    result = {path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")}
    result.update(path.stem for path in root.glob("*.py") if path.stem != "__init__")
    return result


def _late_guarded_compatibility_shims(
    tree: ast.Module,
    entry_import_line: int | None,
) -> list[int]:
    """Find ``if not hasattr(module, name): module.name = ...`` after source import."""

    if entry_import_line is None:
        return []
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or node.lineno <= entry_import_line:
            continue
        guarded = {
            (call.args[0].id, call.args[1].value)
            for call in ast.walk(node.test)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "hasattr"
            and len(call.args) >= 2
            and isinstance(call.args[0], ast.Name)
            and isinstance(call.args[1], ast.Constant)
            and isinstance(call.args[1].value, str)
        }
        if not guarded:
            continue
        for statement in node.body:
            for child in ast.walk(statement):
                if not isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    continue
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and (target.value.id, target.attr) in guarded
                    for target in targets
                ):
                    lines.add(child.lineno)
    return sorted(lines)


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return [alias.name for alias in node.names]


def _asset_literals_without_algorithm_root(tree: ast.Module, algorithm_root: Path) -> set[str]:
    asset_names = {
        path.name
        for path in algorithm_root.rglob("*")
        if (
            path.is_file()
            and path.suffix
            and path.suffix.lower() not in {".py", ".pyc", ".md", ".txt"}
        )
    }
    if not asset_names:
        return set()
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    invalid: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        matched = {name for name in asset_names if name in node.value}
        if not matched:
            continue
        context: ast.AST = node
        while context in parents and not isinstance(context, (ast.Assign, ast.AnnAssign, ast.Return, ast.Expr)):
            context = parents[context]
        if not any(isinstance(child, ast.Name) and child.id == "ALGORITHM_DIR" for child in ast.walk(context)):
            invalid.update(matched)
    return invalid


def _has_runtime_guardrail_fallback(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue
        imports_guardrail = any(
            isinstance(child, (ast.Import, ast.ImportFrom))
            and "runtime_guardrails" in _imported_modules(child)
            for statement in node.body
            for child in ast.walk(statement)
        )
        if imports_guardrail and node.handlers:
            return True
    return False


def _direct_guarded_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        target = node.func
        is_guard = (
            isinstance(target, ast.Name) and target.id == "decode_safe_zip"
        ) or (
            isinstance(target, ast.Attribute) and target.attr == "decode_safe_zip"
        )
        if is_guard and isinstance(node.args[0], ast.Name):
            result.add(node.args[0].id)
    return result


def _base64_decoded_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        target = node.func
        if (
            isinstance(target, ast.Attribute)
            and target.attr in {"b64decode", "decodebytes", "standard_b64decode"}
            and isinstance(node.args[0], ast.Name)
        ):
            result.add(node.args[0].id)
    return result


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )
