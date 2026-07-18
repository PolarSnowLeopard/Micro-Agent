"""Static import-chain evidence for generated artifact dependencies."""

from __future__ import annotations

import ast
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


IMPORT_TO_DISTRIBUTION = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python-headless",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "pil": "pillow",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}
SOURCE_ROOT_NAMES = {"src", "python", "lib"}
IGNORED_LOCAL_MODULES = {"algorithm_loader", "runtime_guardrails"}


def unresolved_import_dependencies(
    algorithm_root: str | Path,
    *,
    source_modules: Iterable[str],
    adapter_path: str | Path | None = None,
    requirement_paths: Iterable[str | Path] = (),
) -> dict[str, dict[str, object]]:
    """Find undeclared imports reachable from exposed local source modules."""
    root = Path(algorithm_root).resolve()
    module_files = _module_file_index(root)
    queue: deque[str] = deque()
    for module in source_modules:
        resolved = _resolve_local_module(module, module_files)
        if resolved:
            queue.append(resolved)

    if adapter_path is not None:
        path = Path(adapter_path)
        if path.is_file():
            for imported in _imports_from_file(path, module_name=""):
                resolved = _resolve_local_module(imported, module_files)
                if resolved:
                    queue.append(resolved)

    external: dict[str, set[str]] = defaultdict(set)
    visited: set[str] = set()
    while queue:
        module = queue.popleft()
        if module in visited:
            continue
        visited.add(module)
        path = module_files[module]
        relative = path.relative_to(root).as_posix()
        for imported in _imports_from_file(path, module_name=module):
            resolved = _resolve_local_module(imported, module_files)
            if resolved:
                if resolved not in visited:
                    queue.append(resolved)
                continue
            import_root = imported.split(".", 1)[0].lower()
            if (
                not import_root
                or import_root in sys.stdlib_module_names
                or import_root in IGNORED_LOCAL_MODULES
            ):
                continue
            external[import_root].add(relative)

    declared = _declared_distributions(requirement_paths)
    unresolved: dict[str, dict[str, object]] = {}
    for import_name, files in sorted(external.items()):
        distribution = IMPORT_TO_DISTRIBUTION.get(
            import_name,
            import_name.replace("_", "-"),
        )
        if canonicalize_name(distribution) in declared:
            continue
        unresolved[import_name] = {
            "distribution": distribution,
            "files": sorted(files)[:8],
        }
    return unresolved


def _module_file_index(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        if any(part in {"tests", "test", "__pycache__", ".venv", "venv"} for part in path.parts):
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        candidates = [parts]
        if parts and parts[0].lower() in SOURCE_ROOT_NAMES:
            candidates.append(parts[1:])
        for candidate in candidates:
            if candidate:
                result.setdefault(".".join(candidate), path)
    return result


def _resolve_local_module(imported: str, module_files: dict[str, Path]) -> str:
    candidate = imported.strip(".")
    while candidate:
        if candidate in module_files:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return ""


def _imports_from_file(path: Path, *, module_name: str) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    optional_import_guards = _optional_import_guard_names(tree)
    imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if _is_optional_or_type_only_import(node, parents, optional_import_guards):
            continue
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
            continue
        base = _resolve_from_base(
            module_name,
            node.module or "",
            node.level,
            is_package=path.name == "__init__.py",
        )
        if base:
            imports.append(base)
        for alias in node.names:
            if alias.name != "*":
                imports.append(".".join(part for part in (base, alias.name) if part))
    return sorted(set(imports))


def _resolve_from_base(
    module_name: str,
    imported: str,
    level: int,
    *,
    is_package: bool,
) -> str:
    if level == 0:
        return imported
    package = module_name if is_package else module_name.rpartition(".")[0]
    parts = package.split(".") if package else []
    keep = max(0, len(parts) - level + 1)
    base = parts[:keep]
    if imported:
        base.extend(imported.split("."))
    return ".".join(base)


def _is_optional_or_type_only_import(
    node: ast.Import | ast.ImportFrom,
    parents: dict[ast.AST, ast.AST],
    optional_import_guards: set[str],
) -> bool:
    current: ast.AST = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If) and _is_type_checking_test(parent.test):
            return True
        if (
            isinstance(parent, ast.If)
            and current in parent.body
            and _references_any_name(parent.test, optional_import_guards)
        ):
            return True
        if isinstance(parent, ast.Try) and any(
            _handler_catches_import_error(handler)
            for handler in parent.handlers
        ):
            return True
        current = parent
    return False


def _optional_import_guard_names(tree: ast.Module) -> set[str]:
    """Find variables populated by APIs whose contract is "module or None"."""
    sympy_aliases = {
        alias.asname or "sympy"
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "sympy"
    }
    external_aliases = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "sympy"
        for alias in node.names
        if alias.name == "external"
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or not _is_sympy_optional_import(
            value.func,
            sympy_aliases=sympy_aliases,
            external_aliases=external_aliases,
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            names.update(
                child.id
                for child in ast.walk(target)
                if isinstance(child, ast.Name)
            )
    return names


def _is_sympy_optional_import(
    node: ast.AST,
    *,
    sympy_aliases: set[str],
    external_aliases: set[str],
) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "import_module"
        and (
            (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "external"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in sympy_aliases
            )
            or (
                isinstance(node.value, ast.Name)
                and node.value.id in external_aliases
            )
        )
    )


def _references_any_name(node: ast.AST, names: set[str]) -> bool:
    return bool(names) and any(
        isinstance(child, ast.Name) and child.id in names
        for child in ast.walk(node)
    )


def _is_type_checking_test(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "TYPE_CHECKING"
    ) or (
        isinstance(node, ast.Attribute)
        and node.attr == "TYPE_CHECKING"
    )


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = {
        child.id
        for child in ast.walk(handler.type)
        if isinstance(child, ast.Name)
    }
    return bool(names & {"ImportError", "ModuleNotFoundError", "Exception"})


def _declared_distributions(paths: Iterable[str | Path]) -> set[str]:
    declared: set[str] = set()
    for value in paths:
        path = Path(value)
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            try:
                requirement = Requirement(line)
            except InvalidRequirement:
                continue
            if not requirement.url:
                declared.add(canonicalize_name(requirement.name))
    return declared
