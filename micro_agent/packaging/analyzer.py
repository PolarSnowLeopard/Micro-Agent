"""Bounded repository inventory for the semantic packaging Agent.

This module never decides which functions become MCP tools. It provides a
complete, reproducible evidence layer so the Agent is not restricted to a
single ``main.py`` or forced into one-tool-per-function heuristics.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
    "env", "node_modules", "dist", "build",
}
DOC_NAMES = {"readme", "readme.md", "readme.rst", "readme.txt"}
ROOT_EVIDENCE_PRIORITY = {
    "main.py": 0,
    "requirements.txt": 1,
    "pyproject.toml": 2,
    "readme": 3,
    "readme.md": 3,
    "readme.rst": 3,
    "readme.txt": 3,
}
ASSET_SUFFIXES = {
    ".bin", ".ckpt", ".joblib", ".model", ".onnx", ".pkl", ".pickle",
    ".pt", ".pth", ".safetensors", ".csv", ".json", ".yaml", ".yml",
}


@dataclass(frozen=True)
class SymbolInfo:
    qualifiedName: str
    module: str
    name: str
    kind: str
    file: str
    line: int
    signature: str
    parameters: list[str]
    requiredParameters: list[str]
    docstring: str
    decorators: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    dispatchBranches: list[dict[str, Any]] = field(default_factory=list)
    isGenerator: bool = False
    failureReturns: list[str] = field(default_factory=list)
    isPublic: bool = True


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int
    kind: str


@dataclass(frozen=True)
class RepositoryIR:
    root: str
    fingerprint: str
    files: list[FileInfo]
    symbols: list[SymbolInfo]
    imports: dict[str, list[str]]
    parseErrors: dict[str, str]
    documentation: dict[str, str]
    entrypointHints: list[str]
    testFiles: list[str]
    assetFiles: list[str]
    truncated: bool = False

    @property
    def known_symbols(self) -> set[str]:
        return {symbol.qualifiedName for symbol in self.symbols}

    @property
    def public_callable_symbols(self) -> set[str]:
        return {
            symbol.qualifiedName
            for symbol in self.symbols
            if symbol.isPublic
            and symbol.kind in {"function", "async_function", "method", "async_method"}
            and symbol.name != "main"
            and not _is_lifecycle_or_operational_name(symbol.name)
        }

    def to_dict(self, *, include_root: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_root:
            data.pop("root", None)
        return data

    def to_json(self, *, include_root: bool = False, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(include_root=include_root), ensure_ascii=False, indent=indent)


class RepositoryAnalyzer:
    def __init__(
        self,
        *,
        max_files: int = 400,
        max_python_bytes: int = 1_000_000,
        max_total_python_bytes: int = 8_000_000,
        max_doc_chars: int = 12_000,
    ) -> None:
        self.max_files = max_files
        self.max_python_bytes = max_python_bytes
        self.max_total_python_bytes = max_total_python_bytes
        self.max_doc_chars = max_doc_chars

    def analyze(self, root: str | Path) -> RepositoryIR:
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise ValueError(f"项目目录不存在: {root_path}")

        candidates = [path for path in root_path.rglob("*") if self._include(path, root_path)]
        candidates.sort(key=lambda path: _candidate_sort_key(path, root_path))
        truncated = len(candidates) > self.max_files
        candidates = candidates[: self.max_files]

        files: list[FileInfo] = []
        symbols: list[SymbolInfo] = []
        imports: dict[str, list[str]] = {}
        parse_errors: dict[str, str] = {}
        docs: dict[str, str] = {}
        entrypoints: list[str] = []
        tests: list[str] = []
        assets: list[str] = []
        digest = hashlib.sha256()
        total_python_bytes = 0

        for path in candidates:
            rel = path.relative_to(root_path).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            kind = _file_kind(path)
            files.append(FileInfo(path=rel, size=stat.st_size, kind=kind))
            digest.update(rel.encode("utf-8"))
            digest.update(str(stat.st_size).encode("ascii"))

            if _is_test_file(path, rel):
                tests.append(rel)
            if path.suffix.lower() in ASSET_SUFFIXES:
                assets.append(rel)
            if _looks_like_entrypoint(path, rel):
                entrypoints.append(rel)

            if path.name.lower() in DOC_NAMES:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")[: self.max_doc_chars]
                    docs[rel] = content
                    digest.update(content.encode("utf-8", errors="replace"))
                except OSError:
                    pass

            if path.suffix.lower() != ".py":
                continue
            if stat.st_size > self.max_python_bytes:
                parse_errors[rel] = f"文件超过单文件分析限制 ({self.max_python_bytes} bytes)"
                continue
            total_python_bytes += stat.st_size
            if total_python_bytes > self.max_total_python_bytes:
                parse_errors[rel] = f"仓库 Python 代码超过分析限制 ({self.max_total_python_bytes} bytes)"
                truncated = True
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                parse_errors[rel] = str(exc)
                continue
            digest.update(source.encode("utf-8", errors="replace"))
            module = _module_name(rel)
            try:
                tree = ast.parse(source, filename=rel)
            except SyntaxError as exc:
                parse_errors[rel] = f"{exc.msg} (line {exc.lineno})"
                continue
            module_imports, module_symbols = _inspect_module(tree, module, rel)
            imports[rel] = module_imports
            symbols.extend(module_symbols)

        if not symbols and not parse_errors:
            parse_errors["<repository>"] = "未发现可分析的 Python 符号"

        return RepositoryIR(
            root=str(root_path),
            fingerprint=digest.hexdigest(),
            files=files,
            symbols=symbols,
            imports=imports,
            parseErrors=parse_errors,
            documentation=docs,
            entrypointHints=sorted(set(entrypoints)),
            testFiles=sorted(set(tests)),
            assetFiles=sorted(set(assets)),
            truncated=truncated,
        )

    @staticmethod
    def _include(path: Path, root: Path) -> bool:
        if not path.is_file() or path.is_symlink():
            return False
        rel_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS for part in rel_parts):
            return False
        if any(part.startswith(".") and part not in {".github"} for part in rel_parts):
            return False
        return True


def _candidate_sort_key(path: Path, root: Path) -> tuple[int, int, str]:
    relative = path.relative_to(root)
    rel = relative.as_posix()
    if len(relative.parts) == 1:
        priority = ROOT_EVIDENCE_PRIORITY.get(relative.name.lower())
        if priority is not None:
            return (0, priority, rel)
    return (1, 0, rel)


def _inspect_module(tree: ast.Module, module: str, rel: str) -> tuple[list[str], list[SymbolInfo]]:
    imports: list[str] = []
    symbols: list[SymbolInfo] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            imports.extend(f"{base}.{alias.name}".strip(".") for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_symbol_from_function(node, module, rel, None))
        elif isinstance(node, ast.ClassDef):
            qualified = f"{module}.{node.name}"
            symbols.append(
                SymbolInfo(
                    qualifiedName=qualified,
                    module=module,
                    name=node.name,
                    kind="class",
                    file=rel,
                    line=node.lineno,
                    signature=node.name,
                    parameters=[],
                    requiredParameters=[],
                    docstring=(ast.get_docstring(node) or "")[:1000],
                    decorators=[_safe_unparse(item) for item in node.decorator_list],
                    calls=[],
                    dispatchBranches=[],
                    isGenerator=False,
                    failureReturns=[],
                    isPublic=not node.name.startswith("_"),
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_symbol_from_function(child, module, rel, node.name))
    return sorted(set(imports)), symbols


def _symbol_from_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    rel: str,
    class_name: str | None,
) -> SymbolInfo:
    prefix = f"{module}.{class_name}" if class_name else module
    qualified = f"{prefix}.{node.name}"
    kind = "async_method" if class_name and isinstance(node, ast.AsyncFunctionDef) else (
        "method" if class_name else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
    )
    return SymbolInfo(
        qualifiedName=qualified,
        module=module,
        name=node.name,
        kind=kind,
        file=rel,
        line=node.lineno,
        signature=_function_signature(node),
        parameters=_function_parameter_names(node),
        requiredParameters=_function_required_parameter_names(node),
        docstring=(ast.get_docstring(node) or "")[:1000],
        decorators=[_safe_unparse(item) for item in node.decorator_list],
        calls=_function_calls(node),
        dispatchBranches=_function_dispatch_branches(node),
        isGenerator=any(isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node)),
        failureReturns=_failure_return_texts(node),
        isPublic=not node.name.startswith("_"),
    )


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = _safe_unparse(node.args)
    returns = f" -> {_safe_unparse(node.returns)}" if node.returns else ""
    return f"{node.name}({args}){returns}"


def _function_parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    names = [arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]]
    return [name for name in names if name not in {"self", "cls"}]


def _function_required_parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    positional = [*node.args.posonlyargs, *node.args.args]
    defaulted_positional = {
        arg.arg for arg in positional[len(positional) - len(node.args.defaults):]
    } if node.args.defaults else set()
    required = [
        arg.arg
        for arg in positional
        if arg.arg not in {"self", "cls"} and arg.arg not in defaulted_positional
    ]
    required.extend(
        arg.arg
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
        if default is None and arg.arg not in {"self", "cls"}
    )
    return required


def _function_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    calls: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _call_name(child.func)
        if name:
            calls.add(name)
    return sorted(calls)


def _function_dispatch_branches(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[dict[str, Any]]:
    """Extract stable literal branches controlled directly by public parameters."""

    parameters = set(_function_parameter_names(node))
    branches: dict[tuple[str, str], dict[str, Any]] = {}
    for child in ast.walk(node):
        if not isinstance(child, ast.If):
            continue
        for parameter, value in _dispatch_comparisons(child.test, parameters):
            key = (parameter, json.dumps(value, ensure_ascii=False, sort_keys=True))
            calls: set[str] = set()
            for statement in child.body:
                for nested in ast.walk(statement):
                    if not isinstance(nested, ast.Call):
                        continue
                    name = _call_name(nested.func)
                    if name:
                        calls.add(name)
            branches[key] = {
                "parameter": parameter,
                "value": value,
                "line": child.lineno,
                "calls": sorted(calls),
            }
    return sorted(
        branches.values(),
        key=lambda item: (item["parameter"], str(item["value"]), item["line"]),
    )


def _dispatch_comparisons(
    expression: ast.expr,
    parameters: set[str],
) -> list[tuple[str, Any]]:
    if isinstance(expression, ast.BoolOp):
        return [
            comparison
            for value in expression.values
            for comparison in _dispatch_comparisons(value, parameters)
        ]
    if not isinstance(expression, ast.Compare) or len(expression.ops) != 1:
        return []
    operator = expression.ops[0]
    right = expression.comparators[0]
    if isinstance(operator, ast.Eq):
        if isinstance(expression.left, ast.Name) and expression.left.id in parameters:
            literal = _literal_value(right)
            return [(expression.left.id, literal)] if literal is not _NO_LITERAL else []
        if isinstance(right, ast.Name) and right.id in parameters:
            literal = _literal_value(expression.left)
            return [(right.id, literal)] if literal is not _NO_LITERAL else []
    if (
        isinstance(operator, ast.In)
        and isinstance(expression.left, ast.Name)
        and expression.left.id in parameters
        and isinstance(right, (ast.List, ast.Tuple, ast.Set))
    ):
        result = []
        for item in right.elts:
            literal = _literal_value(item)
            if literal is not _NO_LITERAL:
                result.append((expression.left.id, literal))
        return result
    return []


_NO_LITERAL = object()


def _literal_value(node: ast.expr) -> Any:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, int, float, bool, type(None))
    ):
        return node.value
    return _NO_LITERAL


def _failure_return_texts(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    result: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
            pairs = {
                key.value: value
                for key, value in zip(child.value.keys, child.value.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            success = pairs.get("success")
            if isinstance(success, ast.Constant) and success.value is False:
                result.add("structured failure: success=false")
        if not isinstance(child, ast.ExceptHandler):
            continue
        for nested in ast.walk(child):
            if not isinstance(nested, ast.Return) or nested.value is None:
                continue
            if isinstance(nested.value, ast.Constant) and isinstance(nested.value.value, str):
                result.add(nested.value.value[:200])
            elif isinstance(nested.value, ast.JoinedStr):
                static = "".join(
                    part.value
                    for part in nested.value.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                ).strip()
                if static:
                    result.add(static[:200])
    return sorted(result)


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _safe_unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _module_name(rel: str) -> str:
    no_suffix = rel[:-3] if rel.endswith(".py") else rel
    parts = list(Path(no_suffix).parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__init__"


def _file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if path.name.lower() in DOC_NAMES or suffix in {".md", ".rst", ".txt"}:
        return "documentation"
    if suffix in ASSET_SUFFIXES:
        return "asset"
    if path.name in {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"}:
        return "dependency"
    return "other"


def _is_test_file(path: Path, rel: str) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.py") or "tests" in Path(rel).parts


def _looks_like_entrypoint(path: Path, rel: str) -> bool:
    if path.name in {"main.py", "app.py", "server.py", "run.py", "start.py", "__main__.py"}:
        return True
    return path.suffix.lower() == ".py" and any(part in {"cli", "scripts", "examples"} for part in Path(rel).parts)


def _is_lifecycle_or_operational_name(name: str) -> bool:
    normalized = name.lower()
    return normalized.startswith(("load_", "init_", "initialize_")) or normalized in {
        "health", "health_check", "readiness", "liveness", "status", "service_status",
        "get_model_info", "model_info", "get_model_metadata", "model_metadata",
        "get_metadata", "metadata",
    }
