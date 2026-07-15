"""Contract tests for the standalone deterministic MCP packager."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from mcp_packager.cli import main as cli_main
from mcp_packager.batch import discover_template_cases, run_template_batch
from mcp_packager.engine import build_package, create_plan, validate_package
from mcp_packager.scaffold import create_scaffold
from mcp_packager.verifier import verify_artifact_static


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_packager_valid"
AMQ_MPMATH_FIXTURE = (
    Path(__file__).parent.parent
    / "benchmarks"
    / "amq_template"
    / "development"
    / "meb_mpmath_001"
)
AMQ_PYDY_FIXTURE = (
    Path(__file__).parent.parent
    / "benchmarks"
    / "amq_template"
    / "development"
    / "meb_pydy_001"
)
AMQ_NETWORKX_FIXTURE = (
    Path(__file__).parent.parent
    / "benchmarks"
    / "amq_template"
    / "development"
    / "meb_networkx_002"
)
AMQ_BIOPYTHON_FIXTURE = (
    Path(__file__).parent.parent
    / "benchmarks"
    / "amq_template"
    / "development"
    / "meb_biopython_002"
)
AMQ_CYTOPUS_FIXTURE = (
    Path(__file__).parent.parent
    / "benchmarks"
    / "amq_template"
    / "development"
    / "meb_cytopus_db_002"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _copy_fixture(destination: Path) -> None:
    for source in FIXTURE.iterdir():
        if source.is_file():
            _write(destination / source.name, source.read_text(encoding="utf-8"))


def test_strict_validation_accepts_versioned_package() -> None:
    report = validate_package(FIXTURE, strict=True)

    assert report.valid is True
    assert report.production_ready is True
    assert report.function is not None
    assert report.function.name == "main_process"
    assert report.function.input_schema()["required"] == ["text"]
    assert report.function.parameters[0].description == "Text to repeat."
    assert report.function.input_schema()["properties"]["text"]["minLength"] == 1
    assert report.function.input_schema()["properties"]["repeat"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 5,
    }
    assert report.function.return_description == "The repeated text and its character count."
    assert report.manifest["service"]["name"] == "repeat-text"
    assert report.source_hash and report.source_hash.startswith("sha256:")


def test_compatibility_mode_accepts_old_template_with_blocking_warnings(tmp_path: Path) -> None:
    source = tmp_path / "algorithm.py"
    _write(
        source,
        '''def main_process(value: int) -> int:
    """Double an integer.

    Args:
        value: Integer to double.

    Returns:
        Doubled integer.
    """
    return value * 2
''',
    )

    report = validate_package(source)

    assert report.valid is True
    assert report.production_ready is False
    assert {issue.code for issue in report.issues} == {
        "MANIFEST_MISSING",
        "REQUIREMENTS_MISSING",
    }
    strict_report = validate_package(source, strict=True)
    assert strict_report.valid is False


def test_compatibility_mode_accepts_legacy_ambiguous_generics_for_preview(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy_algorithm.py"
    _write(
        source,
        '''from typing import Dict, Optional

def main_process(input_data: str, config: Optional[Dict] = None) -> Dict[str, any]:
    """Process data using the documented legacy signature.

    Args:
        input_data: Input text.
        config: Optional legacy configuration.

    Returns:
        Legacy structured result.
    """
    return {"value": input_data, "config": config}
''',
    )

    report = validate_package(source)

    assert report.valid is True
    assert report.production_ready is False
    assert report.function is not None
    assert "AMBIGUOUS_JSON_SCHEMA" in {issue.code for issue in report.issues}

    strict_report = validate_package(source, strict=True)
    assert strict_report.valid is False
    assert "UNSUPPORTED_PARAMETER_TYPE" in {
        issue.code for issue in strict_report.issues
    }


def test_validation_rejects_missing_contract_and_module_state(tmp_path: Path) -> None:
    _write(
        tmp_path / "main.py",
        '''model = load_model()

def main_process(value):
    return model.predict(value)
''',
    )

    report = validate_package(tmp_path)
    codes = {issue.code for issue in report.issues}

    assert report.valid is False
    assert "MODULE_STATE_INITIALIZATION" in codes
    assert "DOCSTRING_MISSING" in codes
    assert "PARAMETER_ANNOTATION_MISSING" in codes
    assert "RETURN_ANNOTATION_MISSING" in codes


def test_validation_distinguishes_model_eval_from_builtin_eval(tmp_path: Path) -> None:
    _write(
        tmp_path / "main.py",
        '''class Model:
    def eval(self) -> None:
        pass


def main_process(value: int) -> int:
    """Run a model-style eval method.

    Args:
        value: Value to return.

    Returns:
        Original value.
    """
    model = Model()
    model.eval()
    return value
''',
    )

    report = validate_package(tmp_path)

    assert "DANGEROUS_CALL" not in {issue.code for issue in report.issues}
    assert report.valid is True

    _write(
        tmp_path / "main.py",
        '''def main_process(expression: str) -> int:
    """Evaluate an expression.

    Args:
        expression: Python expression.

    Returns:
        Expression result.
    """
    return eval(expression)
''',
    )
    report = validate_package(tmp_path)
    assert "DANGEROUS_CALL" in {issue.code for issue in report.issues}


def test_validation_rejects_manifest_test_argument_mismatch(tmp_path: Path) -> None:
    _copy_fixture(tmp_path)
    manifest_path = tmp_path / "ioeb_algorithm.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tests"][0]["arguments"] = {"unknown": "value"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_package(tmp_path, strict=True)
    codes = {issue.code for issue in report.issues}

    assert report.valid is False
    assert "TEST_ARGUMENT_UNKNOWN" in codes
    assert "TEST_ARGUMENT_MISSING" in codes


def test_validation_rejects_expected_output_type_mismatch(tmp_path: Path) -> None:
    _copy_fixture(tmp_path)
    manifest_path = tmp_path / "ioeb_algorithm.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tests"][0]["expected"]["length"] = 8
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_package(tmp_path, strict=True)

    assert report.valid is False
    assert "TEST_EXPECTED_SCHEMA_MISMATCH" in {
        issue.code for issue in report.issues
    }


def test_validation_rejects_invalid_constraints_and_out_of_range_test(
    tmp_path: Path,
) -> None:
    _copy_fixture(tmp_path)
    manifest_path = tmp_path / "ioeb_algorithm.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parameterConstraints"]["repeat"] = {
        "minimum": 5,
        "maximum": 2,
    }
    manifest["tests"][0]["arguments"]["repeat"] = 10
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_package(tmp_path, strict=True)
    codes = {issue.code for issue in report.issues}

    assert report.valid is False
    assert "PARAMETER_CONSTRAINT_CONFLICT" in codes
    assert "TEST_ARGUMENT_SCHEMA_MISMATCH" in codes


def test_validation_rejects_constraint_for_unknown_parameter(tmp_path: Path) -> None:
    _copy_fixture(tmp_path)
    manifest_path = tmp_path / "ioeb_algorithm.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parameterConstraints"]["unknown"] = {"minimum": 1}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_package(tmp_path, strict=True)

    assert report.valid is False
    assert "PARAMETER_CONSTRAINT_UNKNOWN" in {
        issue.code for issue in report.issues
    }


def test_zip_loader_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../main.py", "pass\n")

    report = validate_package(archive)

    assert report.valid is False
    assert report.issues[0].code == "UNSAFE_ARCHIVE_PATH"


def test_build_generates_static_verifiable_artifact(tmp_path: Path) -> None:
    output = tmp_path / "artifact"

    report, plan, artifact = build_package(FIXTURE, output, strict=True)

    assert report.valid is True
    assert plan is not None
    assert plan.service_name == "repeat-text"
    assert artifact == output.resolve()
    assert (output / "algorithm" / "main.py").is_file()
    assert "mcp>=1.27,<2" in (output / "requirements.txt").read_text(encoding="utf-8")
    assert 'mcp.run(transport="streamable-http")' in (output / "server.py").read_text(encoding="utf-8")
    server_source = (output / "server.py").read_text(encoding="utf-8")
    assert "BeforeValidator(_ioeb_validate_text)" in server_source
    assert "WithJsonSchema({'type': 'string', 'maxLength': 100, 'minLength': 1})" in server_source
    assert "Field(description='Text to repeat.'" in server_source
    assert "min_length=1" in server_source
    assert "max_length=100" in server_source
    assert "ge=1" in server_source
    assert "le=5" in server_source
    verifier_source = (output / "_ioeb_verify.py").read_text(encoding="utf-8")
    assert '"name": "invalid-input-probes"' in verifier_source
    assert "report[\"success\"] = case_success and probe_success" in verifier_source
    verification = verify_artifact_static(output)
    assert verification["success"] is True


def test_plan_and_cli_return_nonzero_for_invalid_source(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    _write(source, "def something_else():\n    return 1\n")

    report, plan = create_plan(source)
    exit_code = cli_main(["validate", str(source)])
    output = json.loads(capsys.readouterr().out)

    assert report.valid is False
    assert plan is None
    assert exit_code == 2
    assert output["valid"] is False


def test_amq_template_development_fixture_is_strictly_valid() -> None:
    report, plan = create_plan(AMQ_MPMATH_FIXTURE, strict=True)

    assert report.valid is True
    assert report.production_ready is True
    assert plan is not None
    assert plan.service_name == "arbitrary-precision-math"
    assert plan.tests[0]["name"] == "pi-to-50-decimal-places"
    assert report.manifest["benchmark"]["sampleId"] == "meb_mpmath_001"
    assert plan.function.input_schema()["properties"]["expression"]["enum"] == [
        "pi",
        "e",
    ]
    assert plan.function.input_schema()["properties"]["precision"]["minimum"] == 1
    assert plan.function.input_schema()["properties"]["precision"]["maximum"] == 200


def test_amq_pydy_fixture_covers_object_and_numeric_parameters() -> None:
    report, plan = create_plan(AMQ_PYDY_FIXTURE, strict=True)

    assert report.valid is True
    assert report.production_ready is True
    assert plan is not None
    assert report.manifest["benchmark"]["sampleId"] == "meb_pydy_001"
    properties = plan.function.input_schema()["properties"]
    assert properties["system"]["const"] == "mass_spring_damper"
    initial_conditions = properties["initial_conditions"]
    assert initial_conditions["type"] == "object"
    assert initial_conditions["additionalProperties"] is False
    assert initial_conditions["required"] == [
        "position",
        "velocity",
        "mass",
        "stiffness",
        "damping",
    ]
    assert initial_conditions["properties"]["mass"]["exclusiveMinimum"] == 0.0
    assert initial_conditions["properties"]["damping"]["minimum"] == 0.0
    assert properties["simulation_time"]["minimum"] == 0.1
    assert properties["simulation_time"]["maximum"] == 60.0


def test_nested_object_constraints_reject_invalid_oracle_before_build(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pydy"
    shutil.copytree(AMQ_PYDY_FIXTURE, package)
    manifest_path = package / "ioeb_algorithm.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["tests"][0]["arguments"]["initial_conditions"]["mass"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_package(package, strict=True)

    assert report.valid is False
    assert "TEST_ARGUMENT_SCHEMA_MISMATCH" in {
        issue.code for issue in report.issues
    }
    assert any("mass" in issue.message for issue in report.issues)


def test_nested_object_constraints_reject_invalid_contract(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pydy"
    shutil.copytree(AMQ_PYDY_FIXTURE, package)
    manifest_path = package / "ioeb_algorithm.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    nested = manifest["parameterConstraints"]["initial_conditions"]
    nested["required"].append("not_declared")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_package(package, strict=True)

    assert report.valid is False
    assert "PARAMETER_CONSTRAINT_VALUE_INVALID" in {
        issue.code for issue in report.issues
    }


def test_amq_networkx_fixture_covers_nested_array_constraints() -> None:
    report, plan = create_plan(AMQ_NETWORKX_FIXTURE, strict=True)

    assert report.valid is True
    assert plan is not None
    edges = plan.function.input_schema()["properties"]["edges"]
    assert edges["uniqueItems"] is True
    assert edges["items"]["prefixItems"][2]["exclusiveMinimum"] == 0.0
    assert edges["items"]["prefixItems"][2]["maximum"] == 1000000000.0


def test_amq_biopython_fixture_covers_async_and_literal_array() -> None:
    report, plan = create_plan(AMQ_BIOPYTHON_FIXTURE, strict=True)

    assert report.valid is True
    assert plan is not None
    assert plan.function.is_async is True
    properties = plan.function.input_schema()["properties"]["properties"]
    assert properties["uniqueItems"] is True
    assert properties["items"]["enum"] == [
        "molecular_weight",
        "isoelectric_point",
    ]


def test_amq_cytopus_fixture_covers_constrained_array_items() -> None:
    report, plan = create_plan(AMQ_CYTOPUS_FIXTURE, strict=True)

    assert report.valid is True
    assert plan is not None
    gene_set = plan.function.input_schema()["properties"]["gene_set_a"]
    assert gene_set["items"]["pattern"] == "^[A-Z0-9._-]+$"
    assert gene_set["uniqueItems"] is True


def test_init_scaffold_creates_strictly_valid_production_template(
    tmp_path: Path,
) -> None:
    output = create_scaffold(tmp_path / "algorithm")

    report = validate_package(output, strict=True)

    assert report.valid is True
    assert report.production_ready is True
    assert (output / "README.md").is_file()
    assert report.to_dict()["validationProfile"] == "production-v1"


def test_static_template_batch_reports_first_pass_metrics(tmp_path: Path) -> None:
    root = tmp_path / "cases"
    case = root / "repeat"
    _copy_fixture(case)

    assert discover_template_cases(root) == [case.resolve()]
    batch = run_template_batch(root)

    assert batch["success"] is True
    assert batch["mode"] == "static"
    assert batch["dockerCachePolicy"] is None
    assert batch["summary"]["samples"] == 1
    assert batch["summary"]["firstPassSuccessRate"] == 1.0
    assert batch["summary"]["validationPassRate"] == 1.0
    assert batch["summary"]["verificationPassRate"] == 1.0
    assert batch["summary"]["publishableRate"] == 0.0
    assert batch["summary"]["inputValidationGatePassRate"] == 0.0
    assert batch["samples"][0]["repairAttempts"] == 0
    assert batch["samples"][0]["failureStage"] is None


def test_static_template_batch_scores_expected_rejections(tmp_path: Path) -> None:
    root = tmp_path / "cases"
    accepted = root / "accepted"
    rejected = root / "rejected"
    _copy_fixture(accepted)
    shutil.copytree(
        Path(__file__).parent.parent
        / "benchmarks"
        / "amq_template"
        / "negative"
        / "reject_dangerous_process",
        rejected,
    )

    batch = run_template_batch(root)

    assert batch["success"] is True
    assert batch["summary"]["samples"] == 2
    assert batch["summary"]["positiveSamples"] == 1
    assert batch["summary"]["negativeSamples"] == 1
    assert batch["summary"]["acceptancePassRate"] == 1.0
    assert batch["summary"]["rejectionPassRate"] == 1.0
    negative = next(
        sample for sample in batch["samples"]
        if sample["expectedDisposition"] == "reject"
    )
    assert negative["expectationMet"] is True
    assert negative["verification"] is None
    assert "DANGEROUS_CALL" in negative["observedIssueCodes"]
