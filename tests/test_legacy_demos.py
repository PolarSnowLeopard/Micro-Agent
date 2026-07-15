"""Regression tests for the checksum-pinned historical IoEB demos."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from mcp_packager.engine import build_package, validate_package
from mcp_packager.verifier import verify_artifact_static


ROOT = Path(__file__).resolve().parents[1]
TRACK = ROOT / "benchmarks" / "legacy_demos"


def _load_prepare_module():
    spec = importlib.util.spec_from_file_location(
        "ioeb_legacy_demo_prepare",
        TRACK / "prepare.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREPARE = _load_prepare_module()


def _source_archive(tmp_path: Path, name: str) -> tuple[Path, dict[str, str]]:
    source_root = name
    archive = tmp_path / "archives" / f"{name}.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        stream.writestr(f"{source_root}/source-marker.txt", name)
        if name == "gnn":
            stream.writestr(f"{source_root}/main.py", "# historical entrypoint\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    config = {
        "url": "https://invalid.example.test/source.zip",
        "sha256": digest,
        "root": source_root,
    }
    if name == "gnn":
        config["renameMainTo"] = "inference.py"
    return archive, config


def test_source_catalog_pins_the_three_original_archives() -> None:
    sources = json.loads((TRACK / "sources.json").read_text(encoding="utf-8"))

    assert set(sources) == {"aml", "gnn", "linezolid"}
    assert all(item["url"].startswith("https://ioeb-") for item in sources.values())
    assert all(len(item["sha256"]) == 64 for item in sources.values())


def test_safe_extract_rejects_parent_path(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../escaped.txt", "unsafe")

    with pytest.raises(ValueError, match="unsafe archive path"):
        PREPARE.safe_extract(archive, tmp_path / "output")


@pytest.mark.parametrize("name", ["aml", "gnn", "linezolid"])
def test_prepared_demo_builds_through_the_production_path(
    tmp_path: Path,
    name: str,
) -> None:
    archive, config = _source_archive(tmp_path, name)
    output = tmp_path / "prepared"

    result = PREPARE.prepare_demo(
        name,
        config,
        archive_dir=archive.parent,
        output=output,
    )
    package = Path(result["package"])

    assert (package / "source-marker.txt").read_text(encoding="utf-8") == name
    assert (package / "main.py").is_file()
    assert Path(result["packageZip"]).is_file()
    if name == "gnn":
        assert (package / "inference.py").is_file()

    validation = validate_package(package, strict=True)
    assert validation.valid is True
    assert validation.production_ready is True

    artifact = tmp_path / "artifacts" / name
    _, _, built = build_package(package, artifact, strict=True)
    assert built == artifact
    assert verify_artifact_static(artifact)["success"] is True
