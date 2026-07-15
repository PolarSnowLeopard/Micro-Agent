"""Public data contracts for the standalone packaging engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


SPEC_VERSION = "ioeb.algorithm-package/v1"
PLAN_VERSION = "ioeb.mcp-packaging-plan/v1"
REPORT_VERSION = "ioeb.mcp-validation-report/v1"
ARTIFACT_VERSION = "ioeb.mcp-service-artifact/v1"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: Severity
    path: Optional[str] = None
    line: Optional[int] = None
    hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.line is not None:
            result["line"] = self.line
        if self.hint is not None:
            result["hint"] = self.hint
        return result


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    annotation: str
    schema: Dict[str, Any]
    required: bool
    default: Any = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "name": self.name,
            "annotation": self.annotation,
            "schema": self.schema,
            "required": self.required,
        }
        if not self.required:
            result["default"] = self.default
        if self.description:
            result["description"] = self.description
        return result


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    is_async: bool
    description: str
    parameters: List[ParameterSpec]
    return_annotation: str
    return_schema: Dict[str, Any]
    return_description: str = ""

    def input_schema(self) -> Dict[str, Any]:
        properties = {item.name: item.schema for item in self.parameters}
        required = [item.name for item in self.parameters if item.required]
        schema: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "async": self.is_async,
            "description": self.description,
            "parameters": [item.to_dict() for item in self.parameters],
            "inputSchema": self.input_schema(),
            "returnAnnotation": self.return_annotation,
            "outputSchema": self.return_schema,
            "returnDescription": self.return_description,
        }


@dataclass
class ValidationReport:
    source: str
    package_kind: str
    entry_file: Optional[str]
    strict: bool
    issues: List[Issue] = field(default_factory=list)
    function: Optional[FunctionSpec] = None
    requirements: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    manifest: Dict[str, Any] = field(default_factory=dict)
    source_hash: Optional[str] = None

    @property
    def valid(self) -> bool:
        return not any(issue.severity == Severity.ERROR for issue in self.issues)

    @property
    def production_ready(self) -> bool:
        blocking_warnings = {
            "MANIFEST_MISSING",
            "TEST_CASES_MISSING",
            "TEST_EXPECTED_MISSING",
            "REQUIREMENTS_MISSING",
            "UNPINNED_REQUIREMENT",
            "AMBIGUOUS_JSON_SCHEMA",
        }
        return self.valid and not any(
            issue.code in blocking_warnings for issue in self.issues
        )

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reportVersion": REPORT_VERSION,
            "validationProfile": (
                "production-v1" if self.strict else "legacy-compatible"
            ),
            "valid": self.valid,
            "productionReady": self.production_ready,
            "source": self.source,
            "sourceHash": self.source_hash,
            "packageKind": self.package_kind,
            "entryFile": self.entry_file,
            "strict": self.strict,
            "function": self.function.to_dict() if self.function else None,
            "requirements": self.requirements,
            "imports": self.imports,
            "manifest": self.manifest or None,
            "issues": [issue.to_dict() for issue in self.issues],
            "summary": {
                "errors": sum(
                    issue.severity == Severity.ERROR for issue in self.issues
                ),
                "warnings": sum(
                    issue.severity == Severity.WARNING for issue in self.issues
                ),
                "info": sum(issue.severity == Severity.INFO for issue in self.issues),
            },
        }


@dataclass(frozen=True)
class LoadedPackage:
    source: Path
    root: Path
    entry_file: Path
    package_kind: str


@dataclass(frozen=True)
class PackagingPlan:
    service_name: str
    service_description: str
    source_hash: str
    package_kind: str
    entry_file: str
    function: FunctionSpec
    requirements: List[str]
    tests: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planVersion": PLAN_VERSION,
            "service": {
                "name": self.service_name,
                "description": self.service_description,
            },
            "source": {
                "hash": self.source_hash,
                "packageKind": self.package_kind,
                "entryFile": self.entry_file,
            },
            "tool": self.function.to_dict(),
            "runtime": {
                "language": "python",
                "pythonVersion": "3.11",
                "transport": "streamable-http",
                "endpoint": "/mcp",
                "containerPort": 8000,
                "stateless": True,
            },
            "requirements": self.requirements,
            "tests": self.tests,
        }
