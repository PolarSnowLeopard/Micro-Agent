"""Safe source package loading and reproducible hashing."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator, List, Optional

from mcp_packager.models import LoadedPackage


MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
IGNORED_NAMES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}


class SourcePackageError(ValueError):
    def __init__(self, code: str, message: str, path: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _safe_archive_members(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_FILES:
        raise SourcePackageError(
            "ARCHIVE_TOO_MANY_FILES",
            f"ZIP contains {len(members)} entries; maximum is {MAX_ARCHIVE_FILES}",
        )

    total_size = 0
    safe_members: List[zipfile.ZipInfo] = []
    for member in members:
        normalized = member.filename.replace("\\", "/")
        member_path = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in member_path.parts
            or (member_path.parts and ":" in member_path.parts[0])
        ):
            raise SourcePackageError(
                "UNSAFE_ARCHIVE_PATH",
                f"ZIP entry has an unsafe path: {member.filename}",
                member.filename,
            )

        unix_mode = member.external_attr >> 16
        if stat.S_ISLNK(unix_mode):
            raise SourcePackageError(
                "ARCHIVE_SYMLINK",
                f"Symbolic links are not allowed in ZIP packages: {member.filename}",
                member.filename,
            )

        total_size += member.file_size
        if total_size > MAX_ARCHIVE_BYTES:
            raise SourcePackageError(
                "ARCHIVE_TOO_LARGE",
                "Uncompressed ZIP content exceeds 1 GiB",
            )
        safe_members.append(member)
    return safe_members


def _find_archive_root(extracted: Path) -> Path:
    if (extracted / "main.py").is_file():
        return extracted

    visible = [
        item
        for item in extracted.iterdir()
        if item.name not in {"__MACOSX", ".DS_Store"}
    ]
    if len(visible) == 1 and visible[0].is_dir() and (visible[0] / "main.py").is_file():
        return visible[0]
    raise SourcePackageError(
        "ENTRY_FILE_MISSING",
        "ZIP package must contain main.py at its root or inside one wrapper directory",
    )


def _check_directory_symlinks(root: Path) -> None:
    for current_root, dirs, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in dirs + files:
            candidate = current / name
            if candidate.is_symlink():
                raise SourcePackageError(
                    "SOURCE_SYMLINK",
                    "Symbolic links are not allowed in algorithm packages",
                    candidate.relative_to(root).as_posix(),
                )


@contextmanager
def load_source(source: Path) -> Iterator[LoadedPackage]:
    source = source.expanduser().resolve()
    if not source.exists():
        raise SourcePackageError("SOURCE_NOT_FOUND", f"Source does not exist: {source}")

    if source.is_file() and source.suffix.lower() == ".py":
        yield LoadedPackage(
            source=source,
            root=source.parent,
            entry_file=source,
            package_kind="python-file",
        )
        return

    if source.is_dir():
        _check_directory_symlinks(source)
        entry_file = source / "main.py"
        if not entry_file.is_file():
            raise SourcePackageError(
                "ENTRY_FILE_MISSING", "Project directory must contain main.py"
            )
        yield LoadedPackage(
            source=source,
            root=source,
            entry_file=entry_file,
            package_kind="directory",
        )
        return

    if source.is_file() and source.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="ioeb-mcp-source-") as temp_dir:
            extracted = Path(temp_dir)
            try:
                with zipfile.ZipFile(source) as archive:
                    members = _safe_archive_members(archive)
                    archive.extractall(extracted, members=members)
            except zipfile.BadZipFile as exc:
                raise SourcePackageError("INVALID_ZIP", "Source is not a valid ZIP archive") from exc
            root = _find_archive_root(extracted)
            _check_directory_symlinks(root)
            yield LoadedPackage(
                source=source,
                root=root,
                entry_file=root / "main.py",
                package_kind="zip",
            )
        return

    raise SourcePackageError(
        "UNSUPPORTED_SOURCE",
        "Source must be a Python file, a project directory, or a ZIP archive",
    )


def iter_source_files(package: LoadedPackage) -> Iterator[Path]:
    if package.package_kind == "python-file":
        yield package.entry_file
        for adjacent_name in ("requirements.txt", "ioeb_algorithm.json"):
            adjacent = package.entry_file.parent / adjacent_name
            if adjacent.is_file():
                yield adjacent
        return

    for path in sorted(package.root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package.root)
        if any(part in IGNORED_NAMES for part in relative.parts):
            continue
        yield path


def source_hash(package: LoadedPackage) -> str:
    digest = hashlib.sha256()
    for path in iter_source_files(package):
        if package.package_kind == "python-file" and path == package.entry_file:
            relative = "main.py"
        else:
            relative = path.relative_to(package.root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
