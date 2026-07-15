"""Create a versioned IoEB production algorithm package scaffold."""

from __future__ import annotations

import os
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path


TEMPLATE_VERSION = "ioeb.algorithm-package/v1"
TEMPLATE_RESOURCE = "templates/ioeb_algorithm_v1"


def create_scaffold(output: Path | str, *, force: bool = False) -> Path:
    """Copy the bundled production template into an output directory atomically."""
    destination = Path(output).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        if not force:
            raise FileExistsError(
                f"output path is not an empty directory: {destination}"
            )
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        source = files("mcp_packager").joinpath(TEMPLATE_RESOURCE)
        for item in source.iterdir():
            if item.is_file():
                shutil.copyfile(item, staging / item.name)
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
