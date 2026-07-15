"""Prepare checksum-pinned historical IoEB demos as production-v1 packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parent
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
MAX_FILES = 10_000
MAX_BYTES = 1024 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_FILES:
            raise ValueError(f"archive contains more than {MAX_FILES} entries")
        total = 0
        for member in members:
            normalized = member.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                not normalized
                or normalized.startswith("/")
                or ".." in path.parts
                or (path.parts and ":" in path.parts[0])
            ):
                raise ValueError(f"unsafe archive path: {member.filename}")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f"archive symlink is not allowed: {member.filename}")
            total += member.file_size
            if total > MAX_BYTES:
                raise ValueError("archive exceeds the 1 GiB extraction limit")
        archive.extractall(destination, members=members)


def acquire_archive(name: str, config: dict[str, Any], archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{name}.zip"
    if not archive.is_file():
        urllib.request.urlretrieve(config["url"], archive)
    actual = sha256(archive)
    if actual != config["sha256"]:
        raise ValueError(
            f"source checksum mismatch for {name}: expected {config['sha256']}, got {actual}"
        )
    return archive


def _copy_overlay(name: str, package: Path) -> None:
    overlay = ROOT / "adapters" / name
    if not overlay.is_dir():
        raise ValueError(f"adapter does not exist: {overlay}")
    for source in overlay.rglob("*"):
        if not source.is_file():
            continue
        target = package / source.relative_to(overlay)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _write_zip(package: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(package.rglob("*")):
            if source.is_file():
                archive.write(source, source.relative_to(package).as_posix())


def prepare_demo(
    name: str,
    config: dict[str, Any],
    *,
    archive_dir: Path,
    output: Path,
) -> dict[str, str]:
    archive = acquire_archive(name, config, archive_dir)
    package = output / name
    if package.exists():
        shutil.rmtree(package)
    package.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"ioeb-{name}-source-") as temporary:
        extracted = Path(temporary)
        safe_extract(archive, extracted)
        source_root = extracted / config["root"]
        if not source_root.is_dir():
            raise ValueError(f"declared source root is missing: {config['root']}")
        shutil.copytree(source_root, package)

    rename_target = config.get("renameMainTo")
    if rename_target:
        original_main = package / "main.py"
        if not original_main.is_file():
            raise ValueError(f"{name} source does not contain main.py to rename")
        original_main.rename(package / rename_target)

    _copy_overlay(name, package)
    package_zip = output / f"{name}-production-v1.zip"
    _write_zip(package, package_zip)
    return {
        "name": name,
        "source": str(archive),
        "sourceSha256": config["sha256"],
        "package": str(package),
        "packageZip": str(package_zip),
        "packageZipSha256": sha256(package_zip),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", choices=sorted(SOURCES))
    parser.add_argument("--archive-dir", type=Path, default=Path("/tmp/ioeb-demo-sources"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    names = args.names or sorted(SOURCES)
    results = [
        prepare_demo(
            name,
            SOURCES[name],
            archive_dir=args.archive_dir,
            output=args.output.expanduser().resolve(),
        )
        for name in names
    ]
    print(json.dumps({"packages": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
