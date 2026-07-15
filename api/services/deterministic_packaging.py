"""Compatibility adapter from existing Agent endpoints to ``mcp_packager``."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mcp_packager.engine import build_package, validate_package
from mcp_packager.models import Issue, PackagingPlan, Severity, ValidationReport
from mcp_packager.verifier import verify_artifact_static


@dataclass(frozen=True)
class PackagedService:
    report: ValidationReport
    plan: PackagingPlan
    artifact: Path
    verification: dict[str, Any]


class DeterministicPackagingError(ValueError):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        errors = [
            issue.message
            for issue in report.issues
            if issue.severity.value == "error"
        ]
        message = errors[0] if errors else "算法包不符合 IoEB MCP 封装规范"
        super().__init__(message)


def _zip_has_manifest(source: Path) -> bool:
    try:
        with zipfile.ZipFile(source) as archive:
            files = [
                PurePosixPath(item.filename.replace("\\", "/"))
                for item in archive.infolist()
                if not item.is_dir()
            ]
    except (OSError, zipfile.BadZipFile):
        return False

    files = [
        path
        for path in files
        if path.parts
        and path.parts[0] != "__MACOSX"
        and path.name != ".DS_Store"
    ]
    if any(path.as_posix() == "ioeb_algorithm.json" for path in files):
        return True
    roots = {path.parts[0] for path in files if path.parts}
    return len(roots) == 1 and any(
        len(path.parts) == 2 and path.name == "ioeb_algorithm.json"
        for path in files
    )


def uses_production_profile(source: Path | str) -> bool:
    """Select strict v1 for manifest packages and legacy compatibility otherwise."""
    path = Path(source).expanduser().resolve()
    if path.is_dir():
        return (path / "ioeb_algorithm.json").is_file()
    if path.suffix.lower() == ".zip":
        return _zip_has_manifest(path)
    if path.suffix.lower() == ".py":
        return (path.parent / "ioeb_algorithm.json").is_file()
    return False


def validate_for_frontend(source: Path | str) -> ValidationReport:
    source_path = Path(source).expanduser().resolve()
    report = validate_package(
        source_path,
        strict=uses_production_profile(source_path),
    )
    if not report.valid or report.function is None:
        raise DeterministicPackagingError(report)
    return report


def function_graph(report: ValidationReport) -> dict[str, Any]:
    """Project a validated main_process into the graph shape consumed by Vue."""
    if report.function is None:
        raise DeterministicPackagingError(report)
    function = report.function
    inputs = ", ".join(
        f"{parameter.name}: {parameter.annotation}"
        for parameter in function.parameters
    )
    return {
        "nodes": [
            {
                "id": function.name,
                "label": function.name,
                "input": inputs,
                "output": function.return_annotation,
                "description": function.description,
                "inputSchema": function.input_schema(),
                "outputSchema": function.return_schema,
            }
        ],
        "edges": [],
        "entrypoint": function.name,
    }


def build_for_frontend(
    source: Path | str,
    output: Path | str,
) -> PackagedService:
    source_path = Path(source).expanduser().resolve()
    report, plan, artifact = build_package(
        source_path,
        Path(output),
        strict=uses_production_profile(source_path),
    )
    if plan is None or artifact is None:
        raise DeterministicPackagingError(report)
    verification = verify_artifact_static(artifact)
    if not verification["success"]:
        report.issues.append(
            Issue(
                code="ARTIFACT_STATIC_VERIFICATION_FAILED",
                message="生成的 MCP 服务包未通过静态完整性验证",
                severity=Severity.ERROR,
            )
        )
        raise DeterministicPackagingError(report)
    return PackagedService(report, plan, artifact, verification)
