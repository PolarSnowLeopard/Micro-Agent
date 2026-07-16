"""Runtime helpers copied into generated artifacts for untrusted binary inputs."""

from __future__ import annotations

import base64
import binascii
import io
import stat
import zipfile
from pathlib import PurePosixPath


def decode_safe_zip(
    encoded: str,
    *,
    max_entries: int = 2_000,
    max_compressed_bytes: int = 100_000_000,
    max_uncompressed_bytes: int = 1_000_000_000,
    max_compression_ratio: float = 200.0,
) -> io.BytesIO:
    """Decode and validate a ZIP before legacy algorithm code extracts it."""
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("input is not valid Base64") from exc
    if len(payload) > max_compressed_bytes:
        raise ValueError("compressed ZIP exceeds size limit")

    stream = io.BytesIO(payload)
    try:
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if len(members) > max_entries:
                raise ValueError("ZIP contains too many entries")
            total = 0
            for member in members:
                _validate_member_path(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("ZIP symbolic links are not allowed")
                total += member.file_size
                if total > max_uncompressed_bytes:
                    raise ValueError("uncompressed ZIP exceeds size limit")
                compressed = max(member.compress_size, 1)
                if member.file_size / compressed > max_compression_ratio:
                    raise ValueError("ZIP compression ratio exceeds safety limit")
    except zipfile.BadZipFile as exc:
        raise ValueError("input is not a valid ZIP archive") from exc
    stream.seek(0)
    return stream


def _validate_member_path(name: str) -> None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
        raise ValueError(f"unsafe ZIP member path: {name}")
