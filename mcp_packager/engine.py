"""High-level API that composes loading, validation, planning, and generation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

from mcp_packager.generator import generate_artifact
from mcp_packager.models import (
    Issue,
    PackagingPlan,
    Severity,
    ValidationReport,
)
from mcp_packager.source import SourcePackageError, load_source
from mcp_packager.validator import validate_loaded_package


def _source_error_report(source: Path, strict: bool, exc: SourcePackageError) -> ValidationReport:
    report = ValidationReport(
        source=str(source.expanduser()),
        package_kind="unknown",
        entry_file=None,
        strict=strict,
    )
    report.add(
        Issue(
            code=exc.code,
            message=str(exc),
            severity=Severity.ERROR,
            path=exc.path,
        )
    )
    return report


def validate_package(source: Path | str, *, strict: bool = False) -> ValidationReport:
    source_path = Path(source)
    try:
        with load_source(source_path) as package:
            return validate_loaded_package(package, strict=strict)
    except SourcePackageError as exc:
        return _source_error_report(source_path, strict, exc)


def _slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    if not value:
        return "algorithm-service"
    if value[0].isdigit():
        value = f"algorithm-{value}"
    return value


def _plan_from_report(report: ValidationReport) -> PackagingPlan:
    if not report.valid or report.function is None or report.source_hash is None:
        raise ValueError("cannot create a packaging plan from an invalid source package")
    service = report.manifest.get("service") or {}
    service_name = service.get("name") or _slugify(Path(report.source).stem)
    service_description = service.get("description") or report.function.description
    tests = report.manifest.get("tests") or []
    return PackagingPlan(
        service_name=service_name,
        service_description=service_description,
        source_hash=report.source_hash,
        package_kind=report.package_kind,
        entry_file=report.entry_file or "main.py",
        function=report.function,
        requirements=report.requirements,
        tests=tests,
    )


def create_plan(source: Path | str, *, strict: bool = False) -> Tuple[ValidationReport, Optional[PackagingPlan]]:
    report = validate_package(source, strict=strict)
    return report, _plan_from_report(report) if report.valid else None


def build_package(
    source: Path | str,
    output: Path | str,
    *,
    strict: bool = False,
    force: bool = False,
) -> Tuple[ValidationReport, Optional[PackagingPlan], Optional[Path]]:
    source_path = Path(source)
    try:
        with load_source(source_path) as package:
            report = validate_loaded_package(package, strict=strict)
            if not report.valid:
                return report, None, None
            plan = _plan_from_report(report)
            artifact = generate_artifact(package, plan, Path(output), force=force)
            return report, plan, artifact
    except SourcePackageError as exc:
        return _source_error_report(source_path, strict, exc), None, None
