"""Static validation for the IoEB algorithm submission contract."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Set, Tuple

from mcp_packager.models import (
    SPEC_VERSION,
    FunctionSpec,
    Issue,
    LoadedPackage,
    ParameterSpec,
    Severity,
    ValidationReport,
)
from mcp_packager.source import source_hash


ENTRY_FUNCTION = "main_process"
MANIFEST_NAME = "ioeb_algorithm.json"
REQUIREMENTS_NAME = "requirements.txt"
PLATFORM_REQUIREMENTS = {"mcp", "starlette", "uvicorn", "sse-starlette"}
REQUIREMENT_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;\s*.+)?$"
)
PARAM_DOC_PATTERN = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:\s*(.*)$"
)
SERVICE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
NUMERIC_CONSTRAINTS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
}
STRING_CONSTRAINTS = {"minLength", "maxLength", "pattern"}
ARRAY_CONSTRAINTS = {"minItems", "maxItems", "uniqueItems"}
OBJECT_CONSTRAINTS = {"minProperties", "maxProperties"}
STRUCTURAL_CONSTRAINTS = {
    "items",
    "prefixItems",
    "properties",
    "required",
    "additionalProperties",
}
COMMON_CONSTRAINTS = {"description"}
SUPPORTED_PARAMETER_CONSTRAINTS = (
    NUMERIC_CONSTRAINTS
    | STRING_CONSTRAINTS
    | ARRAY_CONSTRAINTS
    | OBJECT_CONSTRAINTS
    | STRUCTURAL_CONSTRAINTS
    | COMMON_CONSTRAINTS
)


def _issue(
    code: str,
    message: str,
    severity: Severity,
    *,
    path: Optional[str] = None,
    line: Optional[int] = None,
    hint: Optional[str] = None,
) -> Issue:
    return Issue(code, message, severity, path=path, line=line, hint=hint)


class UnsupportedAnnotation(ValueError):
    pass


def _annotation_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _annotation_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _subscript_items(node: ast.Subscript) -> List[ast.AST]:
    value = node.slice
    if isinstance(value, ast.Tuple):
        return list(value.elts)
    return [value]


def annotation_to_schema(
    node: ast.AST, *, allow_legacy_ambiguous: bool = False
) -> Dict[str, Any]:
    """Convert the supported type-annotation subset into JSON Schema."""
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "null"}

    if isinstance(node, ast.Name):
        primitive = {
            "str": {"type": "string"},
            "int": {"type": "integer"},
            "float": {"type": "number"},
            "bool": {"type": "boolean"},
            "None": {"type": "null"},
        }
        if node.id in primitive:
            return primitive[node.id]
        if node.id in {"Any", "any", "object"}:
            if allow_legacy_ambiguous:
                return {}
            raise UnsupportedAnnotation(
                f"{node.id} is ambiguous; use an explicit JSON-compatible type"
            )
        if node.id in {"Dict", "dict", "Mapping"} and allow_legacy_ambiguous:
            return {"type": "object", "additionalProperties": {}}
        if node.id in {"List", "list", "Sequence", "Iterable"} and allow_legacy_ambiguous:
            return {"type": "array", "items": {}}
        raise UnsupportedAnnotation(f"custom type {node.id} is not supported in v1")

    if isinstance(node, ast.Attribute):
        name = _annotation_name(node).split(".")[-1]
        if name == "NoneType":
            return {"type": "null"}
        raise UnsupportedAnnotation(
            f"custom type {_annotation_name(node)} is not supported in v1"
        )

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return {
            "anyOf": [
                annotation_to_schema(
                    node.left, allow_legacy_ambiguous=allow_legacy_ambiguous
                ),
                annotation_to_schema(
                    node.right, allow_legacy_ambiguous=allow_legacy_ambiguous
                ),
            ]
        }

    if not isinstance(node, ast.Subscript):
        raise UnsupportedAnnotation(f"unsupported annotation: {ast.unparse(node)}")

    base = _annotation_name(node.value).split(".")[-1]
    items = _subscript_items(node)
    if base == "Optional" and len(items) == 1:
        return {
            "anyOf": [
                annotation_to_schema(
                    items[0], allow_legacy_ambiguous=allow_legacy_ambiguous
                ),
                {"type": "null"},
            ]
        }
    if base == "Union" and items:
        return {
            "anyOf": [
                annotation_to_schema(
                    item, allow_legacy_ambiguous=allow_legacy_ambiguous
                )
                for item in items
            ]
        }
    if base in {"List", "list", "Sequence", "Iterable"} and len(items) == 1:
        return {
            "type": "array",
            "items": annotation_to_schema(
                items[0], allow_legacy_ambiguous=allow_legacy_ambiguous
            ),
        }
    if base in {"Dict", "dict", "Mapping"} and len(items) == 2:
        key_name = _annotation_name(items[0]).split(".")[-1]
        if key_name != "str":
            raise UnsupportedAnnotation("JSON object keys must be annotated as str")
        return {
            "type": "object",
            "additionalProperties": annotation_to_schema(
                items[1], allow_legacy_ambiguous=allow_legacy_ambiguous
            ),
        }
    if base in {"Tuple", "tuple"} and items:
        if len(items) == 2 and isinstance(items[1], ast.Constant) and items[1].value is Ellipsis:
            return {
                "type": "array",
                "items": annotation_to_schema(
                    items[0], allow_legacy_ambiguous=allow_legacy_ambiguous
                ),
            }
        return {
            "type": "array",
            "prefixItems": [
                annotation_to_schema(
                    item, allow_legacy_ambiguous=allow_legacy_ambiguous
                )
                for item in items
            ],
            "minItems": len(items),
            "maxItems": len(items),
        }
    if base == "Literal" and items:
        values = []
        for item in items:
            try:
                values.append(ast.literal_eval(item))
            except (ValueError, TypeError) as exc:
                raise UnsupportedAnnotation("Literal values must be constants") from exc
        return {"const": values[0]} if len(values) == 1 else {"enum": values}
    raise UnsupportedAnnotation(f"unsupported annotation: {ast.unparse(node)}")


def _annotation_is_ambiguous(node: ast.AST) -> bool:
    ambiguous_values = {"Any", "any", "object"}
    bare_collections = {
        "Dict",
        "dict",
        "Mapping",
        "List",
        "list",
        "Sequence",
        "Iterable",
    }
    if isinstance(node, ast.Name) and node.id in bare_collections:
        return True
    if any(
        isinstance(item, ast.Name) and item.id in ambiguous_values
        for item in ast.walk(node)
    ):
        return True
    for item in ast.walk(node):
        if not isinstance(item, ast.Subscript):
            continue
        if any(
            isinstance(argument, ast.Name) and argument.id in bare_collections
            for argument in _subscript_items(item)
        ):
            return True
    return False


def _doc_sections(docstring: str) -> Tuple[Set[str], Dict[str, str], str]:
    sections: Set[str] = set()
    documented_parameters: Dict[str, str] = {}
    return_description = ""
    current_section: Optional[str] = None
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped in {"Args:", "Arguments:", "Parameters:", "Returns:"}:
            current_section = stripped.rstrip(":")
            sections.add(current_section)
            continue
        if current_section in {"Args", "Arguments", "Parameters"}:
            match = PARAM_DOC_PATTERN.match(line)
            if match:
                documented_parameters[match.group(1)] = match.group(2).strip()
        elif current_section == "Returns" and stripped and not return_description:
            return_description = stripped
    return sections, documented_parameters, return_description


def _parameter_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> List[ast.arg]:
    return list(function.args.posonlyargs) + list(function.args.args) + list(function.args.kwonlyargs)


def _parameter_defaults(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Dict[str, ast.AST]:
    positional = list(function.args.posonlyargs) + list(function.args.args)
    defaults: Dict[str, ast.AST] = {}
    if function.args.defaults:
        for argument, value in zip(positional[-len(function.args.defaults) :], function.args.defaults):
            defaults[argument.arg] = value
    for argument, value in zip(function.args.kwonlyargs, function.args.kw_defaults):
        if value is not None:
            defaults[argument.arg] = value
    return defaults


def _validate_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    entry_path: str,
    report: ValidationReport,
) -> Optional[FunctionSpec]:
    if function.args.vararg or function.args.kwarg:
        report.add(
            _issue(
                "VARIADIC_PARAMETERS_UNSUPPORTED",
                "main_process cannot use *args or **kwargs",
                Severity.ERROR,
                path=entry_path,
                line=function.lineno,
            )
        )

    docstring = ast.get_docstring(function, clean=True) or ""
    if not docstring:
        report.add(
            _issue(
                "DOCSTRING_MISSING",
                "main_process must have a Google-style docstring",
                Severity.ERROR,
                path=entry_path,
                line=function.lineno,
            )
        )
    sections, documented_parameters, return_description = _doc_sections(docstring)
    parameters = _parameter_nodes(function)
    if parameters and not sections.intersection({"Args", "Arguments", "Parameters"}):
        report.add(
            _issue(
                "DOCSTRING_ARGS_MISSING",
                "main_process docstring must contain an Args section",
                Severity.ERROR,
                path=entry_path,
                line=function.lineno,
            )
        )
    if "Returns" not in sections:
        report.add(
            _issue(
                "DOCSTRING_RETURNS_MISSING",
                "main_process docstring must contain a Returns section",
                Severity.ERROR,
                path=entry_path,
                line=function.lineno,
            )
        )

    defaults = _parameter_defaults(function)
    parameter_specs: List[ParameterSpec] = []
    for parameter in parameters:
        if parameter.arg not in documented_parameters:
            report.add(
                _issue(
                    "PARAMETER_NOT_DOCUMENTED",
                    f"Parameter {parameter.arg} is missing from the Args section",
                    Severity.ERROR,
                    path=entry_path,
                    line=parameter.lineno,
                )
            )
        if parameter.annotation is None:
            report.add(
                _issue(
                    "PARAMETER_ANNOTATION_MISSING",
                    f"Parameter {parameter.arg} must have a type annotation",
                    Severity.ERROR,
                    path=entry_path,
                    line=parameter.lineno,
                )
            )
            continue
        try:
            schema = annotation_to_schema(
                parameter.annotation,
                allow_legacy_ambiguous=not report.strict,
            )
        except UnsupportedAnnotation as exc:
            report.add(
                _issue(
                    "UNSUPPORTED_PARAMETER_TYPE",
                    f"Parameter {parameter.arg}: {exc}",
                    Severity.ERROR,
                    path=entry_path,
                    line=parameter.lineno,
                )
            )
            continue
        if not report.strict and _annotation_is_ambiguous(parameter.annotation):
            report.add(
                _issue(
                    "AMBIGUOUS_JSON_SCHEMA",
                    f"Parameter {parameter.arg} uses an ambiguous legacy annotation",
                    Severity.WARNING,
                    path=entry_path,
                    line=parameter.lineno,
                    hint="Use explicit JSON-compatible generic types for production",
                )
            )

        required = parameter.arg not in defaults
        default: Any = None
        if not required:
            try:
                default = ast.literal_eval(defaults[parameter.arg])
            except (ValueError, TypeError):
                report.add(
                    _issue(
                        "NON_LITERAL_DEFAULT",
                        f"Default value for {parameter.arg} must be a literal",
                        Severity.ERROR,
                        path=entry_path,
                        line=parameter.lineno,
                    )
                )
        parameter_specs.append(
            ParameterSpec(
                name=parameter.arg,
                annotation=ast.unparse(parameter.annotation),
                schema=schema,
                required=required,
                default=default,
                description=documented_parameters.get(parameter.arg, ""),
            )
        )

    return_schema: Dict[str, Any] = {}
    return_annotation = ""
    if function.returns is None:
        report.add(
            _issue(
                "RETURN_ANNOTATION_MISSING",
                "main_process must have a return type annotation",
                Severity.ERROR,
                path=entry_path,
                line=function.lineno,
            )
        )
    else:
        return_annotation = ast.unparse(function.returns)
        try:
            return_schema = annotation_to_schema(
                function.returns,
                allow_legacy_ambiguous=not report.strict,
            )
        except UnsupportedAnnotation as exc:
            report.add(
                _issue(
                    "UNSUPPORTED_RETURN_TYPE",
                    f"Return type: {exc}",
                    Severity.ERROR,
                    path=entry_path,
                    line=function.lineno,
                )
            )
        if not report.strict and _annotation_is_ambiguous(function.returns):
            report.add(
                _issue(
                    "AMBIGUOUS_JSON_SCHEMA",
                    "Return type uses an ambiguous legacy annotation",
                    Severity.WARNING,
                    path=entry_path,
                    line=function.lineno,
                    hint="Use an explicit JSON-compatible return type for production",
                )
            )

    if any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(function)):
        report.add(
            _issue(
                "GLOBAL_STATE_USAGE",
                "main_process cannot declare global or nonlocal state",
                Severity.ERROR,
                path=entry_path,
                line=function.lineno,
            )
        )

    description = docstring.splitlines()[0].strip() if docstring else ""
    return FunctionSpec(
        name=ENTRY_FUNCTION,
        is_async=isinstance(function, ast.AsyncFunctionDef),
        description=description,
        parameters=parameter_specs,
        return_annotation=return_annotation,
        return_schema=return_schema,
        return_description=return_description,
    )


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _literal_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    value = node.value
    if value is None:
        return True
    try:
        ast.literal_eval(value)
        return True
    except (ValueError, TypeError):
        return False


def _validate_module_body(tree: ast.Module, entry_path: str, report: ValidationReport) -> None:
    docstring_node = tree.body[0] if tree.body else None
    for node in tree.body:
        if node is docstring_node and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if not _literal_assignment(node):
                report.add(
                    _issue(
                        "MODULE_STATE_INITIALIZATION",
                        "Module-level values must be literals; initialize models and resources inside main_process",
                        Severity.ERROR,
                        path=entry_path,
                        line=node.lineno,
                    )
                )
            continue
        if isinstance(node, ast.If) and _is_main_guard(node):
            continue
        report.add(
            _issue(
                "MODULE_SIDE_EFFECT",
                f"Module-level {type(node).__name__} executes during service import",
                Severity.ERROR,
                path=entry_path,
                line=getattr(node, "lineno", None),
            )
        )


def _collect_imports(tree: ast.Module) -> List[str]:
    imports: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return sorted(imports)


def _validate_dangerous_calls(tree: ast.Module, entry_path: str, report: ValidationReport) -> None:
    dangerous_names = {"eval", "exec", "compile", "__import__"}
    dangerous_qualified_names = {
        f"builtins.{item}" for item in dangerous_names
    }
    warned: Set[Tuple[str, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _annotation_name(node.func)
        reason: Optional[str] = None
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in dangerous_names
        ) or name in dangerous_qualified_names:
            reason = f"dynamic execution call {name} requires manual review"
        elif name in {"os.system", "os.popen"} or name.startswith("subprocess."):
            reason = f"process execution call {name} requires manual review"
        if reason and (name, node.lineno) not in warned:
            warned.add((name, node.lineno))
            report.add(
                _issue(
                    "DANGEROUS_CALL",
                    reason,
                    Severity.ERROR,
                    path=entry_path,
                    line=node.lineno,
                )
            )


def _requirement_name(line: str) -> str:
    name = re.split(r"[<>=!~\[\s;]", line, maxsplit=1)[0]
    return name.lower().replace("_", "-")


def _read_requirements(package: LoadedPackage, report: ValidationReport) -> List[str]:
    if package.package_kind == "python-file":
        requirements_path = package.entry_file.parent / REQUIREMENTS_NAME
        if not requirements_path.is_file():
            report.add(
                _issue(
                    "REQUIREMENTS_MISSING",
                    "Single-file submission has no requirements.txt; external dependencies cannot be reproduced",
                    Severity.ERROR if report.strict else Severity.WARNING,
                    hint="Submit a ZIP project with pinned requirements for production packaging",
                )
            )
            return []
    else:
        requirements_path = package.root / REQUIREMENTS_NAME
        if not requirements_path.is_file():
            report.add(
                _issue(
                    "REQUIREMENTS_MISSING",
                    "Project package has no requirements.txt",
                    Severity.ERROR if report.strict else Severity.WARNING,
                    path=REQUIREMENTS_NAME,
                )
            )
            return []

    requirements: List[str] = []
    seen_requirements: Set[str] = set()
    for number, raw_line in enumerate(requirements_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = _requirement_name(line)
        if name in PLATFORM_REQUIREMENTS:
            report.add(
                _issue(
                    "PLATFORM_DEPENDENCY_IGNORED",
                    f"{name} is managed by the MCP runtime and will be replaced by the platform pin",
                    Severity.WARNING,
                    path=REQUIREMENTS_NAME,
                    line=number,
                )
            )
            continue
        if name in seen_requirements:
            report.add(
                _issue(
                    "DUPLICATE_REQUIREMENT",
                    f"Requirement {name} is declared more than once",
                    Severity.ERROR,
                    path=REQUIREMENTS_NAME,
                    line=number,
                )
            )
            continue
        if line.startswith(("-", "http://", "https://", "git+", ".", "/")):
            report.add(
                _issue(
                    "UNSAFE_REQUIREMENT",
                    f"Requirement must use a package name and exact version: {line}",
                    Severity.ERROR,
                    path=REQUIREMENTS_NAME,
                    line=number,
                )
            )
            continue
        if not REQUIREMENT_PATTERN.match(line):
            report.add(
                _issue(
                    "UNPINNED_REQUIREMENT",
                    f"Requirement must be exactly pinned with ==: {line}",
                    Severity.ERROR if report.strict else Severity.WARNING,
                    path=REQUIREMENTS_NAME,
                    line=number,
                )
            )
        seen_requirements.add(name)
        requirements.append(line)
    return requirements


def _load_manifest(package: LoadedPackage, report: ValidationReport) -> Dict[str, Any]:
    if package.package_kind == "python-file":
        manifest_path = package.entry_file.parent / MANIFEST_NAME
    else:
        manifest_path = package.root / MANIFEST_NAME
    if not manifest_path.is_file():
        report.add(
            _issue(
                "MANIFEST_MISSING",
                f"{MANIFEST_NAME} is required for reproducible service metadata and test cases",
                Severity.ERROR if report.strict else Severity.WARNING,
                path=MANIFEST_NAME,
            )
        )
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.add(
            _issue(
                "MANIFEST_INVALID_JSON",
                f"Cannot parse {MANIFEST_NAME}: {exc}",
                Severity.ERROR,
                path=MANIFEST_NAME,
            )
        )
        return {}
    if not isinstance(manifest, dict):
        report.add(
            _issue(
                "MANIFEST_INVALID",
                f"{MANIFEST_NAME} root must be a JSON object",
                Severity.ERROR,
                path=MANIFEST_NAME,
            )
        )
        return {}
    if manifest.get("specVersion") != SPEC_VERSION:
        report.add(
            _issue(
                "MANIFEST_VERSION_UNSUPPORTED",
                f"specVersion must be {SPEC_VERSION}",
                Severity.ERROR,
                path=MANIFEST_NAME,
            )
        )
    service = manifest.get("service")
    if not isinstance(service, dict):
        report.add(
            _issue("SERVICE_METADATA_MISSING", "manifest.service must be an object", Severity.ERROR, path=MANIFEST_NAME)
        )
    else:
        name = service.get("name")
        if not isinstance(name, str) or not SERVICE_NAME_PATTERN.fullmatch(name):
            report.add(
                _issue(
                    "SERVICE_NAME_INVALID",
                    "service.name must use lowercase kebab-case",
                    Severity.ERROR,
                    path=MANIFEST_NAME,
                )
            )
        if not isinstance(service.get("description"), str) or not service.get("description", "").strip():
            report.add(
                _issue(
                    "SERVICE_DESCRIPTION_MISSING",
                    "service.description must be a non-empty string",
                    Severity.ERROR,
                    path=MANIFEST_NAME,
                )
            )
    if manifest.get("entrypoint", f"main:{ENTRY_FUNCTION}") != f"main:{ENTRY_FUNCTION}":
        report.add(
            _issue(
                "ENTRYPOINT_UNSUPPORTED",
                f"entrypoint must be main:{ENTRY_FUNCTION} in v1",
                Severity.ERROR,
                path=MANIFEST_NAME,
            )
        )
    tests = manifest.get("tests")
    if not isinstance(tests, list) or not tests:
        report.add(
            _issue(
                "TEST_CASES_MISSING",
                "manifest.tests must contain at least one verification case",
                Severity.ERROR if report.strict else Severity.WARNING,
                path=MANIFEST_NAME,
            )
        )
    else:
        for index, case in enumerate(tests):
            if not isinstance(case, dict) or not isinstance(case.get("arguments"), dict):
                report.add(
                    _issue(
                        "TEST_CASE_INVALID",
                        f"tests[{index}] must be an object with an arguments object",
                        Severity.ERROR,
                        path=MANIFEST_NAME,
                    )
                )
            elif "expected" not in case:
                report.add(
                    _issue(
                        "TEST_EXPECTED_MISSING",
                        f"tests[{index}] must define expected for differential verification",
                        Severity.ERROR if report.strict else Severity.WARNING,
                        path=MANIFEST_NAME,
                    )
                )
    return manifest


def _schema_types(schema: Dict[str, Any]) -> Set[str]:
    schema_type = schema.get("type")
    types = {schema_type} if isinstance(schema_type, str) else set()
    for alternative in schema.get("anyOf", []):
        if isinstance(alternative, dict):
            types.update(_schema_types(alternative))
    return types


def _constraint_value_is_valid(name: str, value: Any) -> bool:
    if name in NUMERIC_CONSTRAINTS:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        return name != "multipleOf" or value > 0
    if name in {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
    }:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if name == "uniqueItems":
        return isinstance(value, bool)
    if name == "description":
        return isinstance(value, str) and bool(value.strip())
    if name == "pattern":
        if not isinstance(value, str):
            return False
        try:
            re.compile(value)
        except re.error:
            return False
        return True
    return False


def _constraint_supported_for_types(name: str, types: Set[str]) -> bool:
    if name in NUMERIC_CONSTRAINTS:
        return bool(types.intersection({"integer", "number"}))
    if name in STRING_CONSTRAINTS:
        return "string" in types
    if name in ARRAY_CONSTRAINTS:
        return "array" in types
    if name in OBJECT_CONSTRAINTS:
        return "object" in types
    if name == "description":
        return True
    return False


def _constraint_conflict(schema: Dict[str, Any]) -> Optional[str]:
    for minimum_name, maximum_name in (
        ("minimum", "maximum"),
        ("exclusiveMinimum", "exclusiveMaximum"),
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
        ("minProperties", "maxProperties"),
    ):
        minimum = schema.get(minimum_name)
        maximum = schema.get(maximum_name)
        if minimum is not None and maximum is not None and minimum > maximum:
            return f"{minimum_name} cannot exceed {maximum_name}"

    lower = schema.get("exclusiveMinimum", schema.get("minimum"))
    upper = schema.get("exclusiveMaximum", schema.get("maximum"))
    if lower is not None and upper is not None:
        exclusive = "exclusiveMinimum" in schema or "exclusiveMaximum" in schema
        if lower > upper or (exclusive and lower == upper):
            return "numeric lower bound must be smaller than the upper bound"
    return None


def _schema_value_error(value: Any, schema: Dict[str, Any]) -> Optional[str]:
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        if any(
            isinstance(alternative, dict)
            and _schema_value_error(value, alternative) is None
            for alternative in alternatives
        ):
            return None
        return "does not match any allowed type"

    if "const" in schema and value != schema["const"]:
        return f"must equal {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return "is not one of the allowed values"

    schema_type = schema.get("type")
    type_matches = {
        "null": value is None,
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }
    if schema_type in type_matches and not type_matches[schema_type]:
        return f"must be {schema_type}"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"must be <= {schema['maximum']}"
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return f"must be > {schema['exclusiveMinimum']}"
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            return f"must be < {schema['exclusiveMaximum']}"
        if "multipleOf" in schema and value % schema["multipleOf"] != 0:
            return f"must be a multiple of {schema['multipleOf']}"

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"length must be >= {schema['minLength']}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"length must be <= {schema['maxLength']}"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return f"must match pattern {schema['pattern']}"

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"item count must be >= {schema['minItems']}"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"item count must be <= {schema['maxItems']}"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _schema_value_error(item, item_schema)
                if error:
                    return f"item {index} {error}"
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, item_schema in enumerate(prefix_items):
                if index >= len(value) or not isinstance(item_schema, dict):
                    continue
                error = _schema_value_error(value[index], item_schema)
                if error:
                    return f"item {index} {error}"
        if schema.get("uniqueItems"):
            for index, item in enumerate(value):
                if item in value[:index]:
                    return "items must be unique"

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [name for name in required if name not in value]
        if missing:
            return f"is missing required properties: {', '.join(missing)}"
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            return f"property count must be >= {schema['minProperties']}"
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            return f"property count must be <= {schema['maxProperties']}"
        for name, item in value.items():
            item_schema = properties.get(name)
            if isinstance(item_schema, dict):
                error = _schema_value_error(item, item_schema)
                if error:
                    return f"property {name} {error}"
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                return f"contains unsupported property {name}"
            if isinstance(additional, dict):
                error = _schema_value_error(item, additional)
                if error:
                    return f"property {name} {error}"
    return None


def _constraint_issue(
    report: ValidationReport,
    code: str,
    message: str,
) -> None:
    report.add(_issue(code, message, Severity.ERROR, path=MANIFEST_NAME))


def _merge_constraint_schema(
    base_schema: Dict[str, Any],
    constraints: Dict[str, Any],
    *,
    location: str,
    report: ValidationReport,
) -> Dict[str, Any]:
    """Validate and recursively merge a constrained JSON-schema subset."""
    schema = dict(base_schema)
    types = _schema_types(schema)

    for name in sorted(set(constraints) - SUPPORTED_PARAMETER_CONSTRAINTS):
        _constraint_issue(
            report,
            "PARAMETER_CONSTRAINT_UNSUPPORTED",
            f"Unsupported constraint for {location}: {name}",
        )

    for name in sorted(
        set(constraints).intersection(
            NUMERIC_CONSTRAINTS
            | STRING_CONSTRAINTS
            | ARRAY_CONSTRAINTS
            | OBJECT_CONSTRAINTS
            | COMMON_CONSTRAINTS
        )
    ):
        value = constraints[name]
        if not _constraint_supported_for_types(name, types):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_TYPE_MISMATCH",
                f"Constraint {name} is incompatible with {location}",
            )
            continue
        if not _constraint_value_is_valid(name, value):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_VALUE_INVALID",
                f"Constraint {location}.{name} has an invalid value",
            )
            continue
        schema[name] = value

    if "items" in constraints:
        item_constraints = constraints["items"]
        item_schema = schema.get("items")
        if "array" not in types or not isinstance(item_schema, dict):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_TYPE_MISMATCH",
                f"Constraint items is incompatible with {location}",
            )
        elif not isinstance(item_constraints, dict):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_VALUE_INVALID",
                f"Constraint {location}.items must be an object",
            )
        else:
            schema["items"] = _merge_constraint_schema(
                item_schema,
                item_constraints,
                location=f"{location}.items",
                report=report,
            )

    if "prefixItems" in constraints:
        prefix_constraints = constraints["prefixItems"]
        prefix_schema = schema.get("prefixItems")
        valid_fragments = isinstance(prefix_constraints, list) and all(
            isinstance(item, dict) for item in prefix_constraints
        )
        if "array" not in types or not isinstance(prefix_schema, list):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_TYPE_MISMATCH",
                f"Constraint prefixItems is incompatible with {location}",
            )
        elif not valid_fragments or len(prefix_constraints) != len(prefix_schema):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_VALUE_INVALID",
                f"Constraint {location}.prefixItems must match the annotated tuple length",
            )
        else:
            schema["prefixItems"] = [
                _merge_constraint_schema(
                    item_schema,
                    item_constraints,
                    location=f"{location}.prefixItems[{index}]",
                    report=report,
                )
                for index, (item_schema, item_constraints) in enumerate(
                    zip(prefix_schema, prefix_constraints)
                )
            ]

    if "properties" in constraints:
        property_constraints = constraints["properties"]
        if "object" not in types:
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_TYPE_MISMATCH",
                f"Constraint properties is incompatible with {location}",
            )
        elif not isinstance(property_constraints, dict) or not all(
            isinstance(name, str)
            and bool(name)
            and isinstance(fragment, dict)
            for name, fragment in property_constraints.items()
        ):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_VALUE_INVALID",
                f"Constraint {location}.properties must map names to objects",
            )
        else:
            existing = schema.get("properties")
            properties = dict(existing) if isinstance(existing, dict) else {}
            additional = schema.get("additionalProperties")
            for name, fragment in property_constraints.items():
                inferred = properties.get(name)
                if not isinstance(inferred, dict):
                    inferred = dict(additional) if isinstance(additional, dict) else {}
                properties[name] = _merge_constraint_schema(
                    inferred,
                    fragment,
                    location=f"{location}.properties.{name}",
                    report=report,
                )
            schema["properties"] = properties

    if "required" in constraints:
        required = constraints["required"]
        properties = schema.get("properties")
        valid_required = (
            isinstance(required, list)
            and all(isinstance(name, str) and bool(name) for name in required)
            and len(set(required)) == len(required)
        )
        if "object" not in types:
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_TYPE_MISMATCH",
                f"Constraint required is incompatible with {location}",
            )
        elif not valid_required:
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_VALUE_INVALID",
                f"Constraint {location}.required must contain unique property names",
            )
        elif not isinstance(properties, dict) or not set(required).issubset(properties):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_VALUE_INVALID",
                f"Constraint {location}.required must reference declared properties",
            )
        else:
            schema["required"] = required

    if "additionalProperties" in constraints:
        additional = constraints["additionalProperties"]
        if "object" not in types:
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_TYPE_MISMATCH",
                f"Constraint additionalProperties is incompatible with {location}",
            )
        elif not isinstance(additional, bool):
            _constraint_issue(
                report,
                "PARAMETER_CONSTRAINT_VALUE_INVALID",
                f"Constraint {location}.additionalProperties must be boolean",
            )
        else:
            schema["additionalProperties"] = additional

    conflict = _constraint_conflict(schema)
    if conflict:
        _constraint_issue(
            report,
            "PARAMETER_CONSTRAINT_CONFLICT",
            f"Constraints for {location}: {conflict}",
        )
    return schema


def _apply_parameter_constraints(report: ValidationReport) -> None:
    if not report.function or not report.manifest:
        return
    declared = report.manifest.get("parameterConstraints", {})
    if not isinstance(declared, dict):
        report.add(
            _issue(
                "PARAMETER_CONSTRAINTS_INVALID",
                "manifest.parameterConstraints must be an object",
                Severity.ERROR,
                path=MANIFEST_NAME,
            )
        )
        return

    parameters = {parameter.name: parameter for parameter in report.function.parameters}
    updated: List[ParameterSpec] = []
    for parameter in report.function.parameters:
        constraints = declared.get(parameter.name, {})
        if not isinstance(constraints, dict):
            report.add(
                _issue(
                    "PARAMETER_CONSTRAINTS_INVALID",
                    f"parameterConstraints.{parameter.name} must be an object",
                    Severity.ERROR,
                    path=MANIFEST_NAME,
                )
            )
            updated.append(parameter)
            continue

        schema = _merge_constraint_schema(
            parameter.schema,
            constraints,
            location=parameter.name,
            report=report,
        )
        if not parameter.required:
            default_error = _schema_value_error(parameter.default, schema)
            if default_error:
                report.add(
                    _issue(
                        "PARAMETER_DEFAULT_SCHEMA_MISMATCH",
                        f"Default for {parameter.name} {default_error}",
                        Severity.ERROR,
                        path=report.entry_file,
                    )
                )
        updated.append(replace(parameter, schema=schema))

    unknown = sorted(set(declared) - set(parameters))
    for name in unknown:
        report.add(
            _issue(
                "PARAMETER_CONSTRAINT_UNKNOWN",
                f"parameterConstraints references unknown parameter: {name}",
                Severity.ERROR,
                path=MANIFEST_NAME,
            )
        )
    report.function = replace(report.function, parameters=updated)


def _validate_test_arguments(report: ValidationReport) -> None:
    if not report.function or not report.manifest:
        return
    parameters = {parameter.name: parameter for parameter in report.function.parameters}
    required = {name for name, parameter in parameters.items() if parameter.required}
    tests = report.manifest.get("tests") or []
    for index, case in enumerate(tests):
        if not isinstance(case, dict) or not isinstance(case.get("arguments"), dict):
            continue
        arguments = case["arguments"]
        unknown = sorted(set(arguments) - set(parameters))
        missing = sorted(required - set(arguments))
        if unknown:
            report.add(
                _issue(
                    "TEST_ARGUMENT_UNKNOWN",
                    f"tests[{index}] contains unknown arguments: {', '.join(unknown)}",
                    Severity.ERROR,
                    path=MANIFEST_NAME,
                )
            )
        if missing:
            report.add(
                _issue(
                    "TEST_ARGUMENT_MISSING",
                    f"tests[{index}] is missing required arguments: {', '.join(missing)}",
                    Severity.ERROR,
                    path=MANIFEST_NAME,
                )
            )
        for name in sorted(set(arguments).intersection(parameters)):
            value_error = _schema_value_error(arguments[name], parameters[name].schema)
            if value_error:
                report.add(
                    _issue(
                        "TEST_ARGUMENT_SCHEMA_MISMATCH",
                        f"tests[{index}].arguments.{name} {value_error}",
                        Severity.ERROR,
                        path=MANIFEST_NAME,
                    )
                )
        if "expected" in case:
            expected_error = _schema_value_error(
                case["expected"], report.function.return_schema
            )
            if expected_error:
                report.add(
                    _issue(
                        "TEST_EXPECTED_SCHEMA_MISMATCH",
                        f"tests[{index}].expected {expected_error}",
                        Severity.ERROR,
                        path=MANIFEST_NAME,
                    )
                )


def validate_loaded_package(package: LoadedPackage, *, strict: bool = False) -> ValidationReport:
    entry_path = "main.py" if package.package_kind != "python-file" else package.entry_file.name
    report = ValidationReport(
        source=str(package.source),
        package_kind=package.package_kind,
        entry_file=entry_path,
        strict=strict,
        source_hash=source_hash(package),
    )

    try:
        source_text = package.entry_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.add(
            _issue(
                "SOURCE_ENCODING_INVALID",
                "Entry file must be valid UTF-8",
                Severity.ERROR,
                path=entry_path,
            )
        )
        return report
    try:
        tree = ast.parse(source_text, filename=entry_path)
    except SyntaxError as exc:
        report.add(
            _issue(
                "PYTHON_SYNTAX_ERROR",
                exc.msg,
                Severity.ERROR,
                path=entry_path,
                line=exc.lineno,
            )
        )
        return report

    _validate_module_body(tree, entry_path, report)
    _validate_dangerous_calls(tree, entry_path, report)
    report.imports = _collect_imports(tree)

    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == ENTRY_FUNCTION
    ]
    if not functions:
        report.add(
            _issue(
                "ENTRY_FUNCTION_MISSING",
                f"Entry file must define a top-level {ENTRY_FUNCTION} function",
                Severity.ERROR,
                path=entry_path,
            )
        )
    elif len(functions) > 1:
        report.add(
            _issue(
                "ENTRY_FUNCTION_DUPLICATED",
                f"Entry file defines {ENTRY_FUNCTION} more than once",
                Severity.ERROR,
                path=entry_path,
            )
        )
    else:
        report.function = _validate_function(functions[0], entry_path, report)

    report.requirements = _read_requirements(package, report)
    report.manifest = _load_manifest(package, report)
    _apply_parameter_constraints(report)
    _validate_test_arguments(report)
    return report
