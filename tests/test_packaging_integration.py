"""System compatibility tests for the deterministic packaging endpoints."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.services.deterministic_packaging import (
    DeterministicPackagingError,
    function_graph,
    uses_production_profile,
    validate_for_frontend,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_packager_valid"


def _fixture_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in FIXTURE.iterdir():
            if path.is_file():
                archive.writestr(
                    f"algorithm/{path.name}",
                    path.read_bytes(),
                )
    return buffer.getvalue()


def _sse_final(response) -> dict:
    events = []
    for block in response.text.split("\n\n"):
        if block.startswith("data: "):
            events.append(json.loads(block[6:]))
    return next(item["final_results"] for item in events if item.get("is_final_result"))


def test_adapter_selects_strict_manifest_profile_and_projects_graph(tmp_path: Path) -> None:
    archive = tmp_path / "algorithm.zip"
    archive.write_bytes(_fixture_zip())

    report = validate_for_frontend(archive)
    graph = function_graph(report)

    assert uses_production_profile(archive) is True
    assert report.strict is True
    assert graph["nodes"][0]["label"] == "main_process"
    assert graph["nodes"][0]["inputSchema"]["required"] == ["text"]


def test_adapter_ignores_macos_zip_metadata_when_selecting_profile(tmp_path: Path) -> None:
    archive = tmp_path / "algorithm.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        for path in FIXTURE.iterdir():
            if path.is_file():
                stream.writestr(f"algorithm/{path.name}", path.read_bytes())
        stream.writestr("__MACOSX/algorithm/._main.py", b"")

    report = validate_for_frontend(archive)

    assert uses_production_profile(archive) is True
    assert report.strict is True


def test_adapter_preserves_legacy_single_file_template(tmp_path: Path) -> None:
    source = tmp_path / "legacy.py"
    source.write_text(
        '''def main_process(value: int) -> dict[str, str]:
    """Render an integer.

    Args:
        value: Integer to render.

    Returns:
        Rendered integer.
    """
    return {"value": str(value)}
''',
        encoding="utf-8",
    )

    report = validate_for_frontend(source)

    assert report.valid is True
    assert report.strict is False
    assert report.production_ready is False


def test_adapter_rejects_invalid_algorithm(tmp_path: Path) -> None:
    source = tmp_path / "invalid.py"
    source.write_text("def wrong_name():\n    return 1\n", encoding="utf-8")

    with pytest.raises(DeterministicPackagingError) as exc_info:
        validate_for_frontend(source)

    assert "ENTRY_FUNCTION_MISSING" in {
        issue.code for issue in exc_info.value.report.issues
    }


def test_existing_frontend_endpoints_return_graph_and_deployable_zip() -> None:
    client = TestClient(app)
    payload = _fixture_zip()

    analysis = client.post(
        "/api/agent/code_analysis",
        files={"file": ("algorithm.zip", payload, "application/zip")},
    )
    assert analysis.status_code == 200
    analysis_final = _sse_final(analysis)
    assert analysis_final["function"]["nodes"][0]["label"] == "main_process"
    assert analysis_final["packaging_validation"]["valid"] is True

    packaging = client.post(
        "/api/agent/service_packaging",
        files={"file": ("algorithm.zip", payload, "application/zip")},
    )
    assert packaging.status_code == 200
    packaging_final = _sse_final(packaging)
    assert packaging_final["packaging_verification"]["success"] is True
    archive_bytes = base64.b64decode(packaging_final["service_package"]["content"])
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
    assert any(name.endswith("/server.py") for name in names)
    assert any(name.endswith("/Dockerfile") for name in names)
    assert any(name.endswith("/docker-compose.yml") for name in names)
    assert any(name.endswith("/ioeb-service.json") for name in names)


def test_existing_frontend_endpoint_returns_actionable_validation_error() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/agent/code_analysis",
        files={"file": ("invalid.py", b"def wrong_name():\n    return 1\n", "text/x-python")},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"]
    assert "ENTRY_FUNCTION_MISSING" in {
        issue["code"] for issue in detail["validation"]["issues"]
    }
