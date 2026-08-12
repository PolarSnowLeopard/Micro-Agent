"""PoC tests for CWE-22 path traversal in :func:`api.services.files.save_upload`.

Vulnerability
-------------
``save_upload`` builds its destination path with::

    dest = dest_dir / f"{ts}_{upload.filename or 'upload'}"

The ``upload.filename`` value comes straight from the multipart ``filename``
header on every endpoint that accepts a file (``/api/agent/code_analysis``,
``/api/agent/service_packaging``, ``/api/agent/aml_auto_generate``,
``/api/agent/aml_report``, …). None of those endpoints require authentication,
and starlette / python-multipart preserves slashes/``..`` verbatim.

Two concrete write-outside-workspace primitives exist on the unfixed code:

1. **Collision with an existing directory** — when the workspace already
   contains a directory whose name matches ``"<ts>_<first-segment-of-filename>"``
   (a very common situation: the service creates ``<ts>_extracted`` /
   ``<ts>_marker`` dirs over its lifetime, ``ts`` collisions can be raced
   inside a one-second window, and prior uploads can prepare matching dirs),
   a filename like ``marker/../../ESCAPED`` resolves to a path outside the
   workspace. ``write_bytes`` succeeds and arbitrary attacker bytes land on
   disk at the chosen location.

2. **On Windows** the backslash is a path separator; ``..\\..\\evil`` escapes
   immediately on the first call.

Even when neither primitive triggers, an unsanitised filename pollutes the
workspace with weird relative names and breaks the ``cleanup_paths`` logic.

These tests assert that the fix sanitises the filename so the saved path is
always a single component inside ``dest_dir``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path


class _FakeUpload:
    """Minimal stand-in for ``starlette.datastructures.UploadFile``."""

    def __init__(self, filename: str, content: bytes = b"x") -> None:
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


def _run(coro):
    if sys.version_info < (3, 10):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return asyncio.run(coro)


def _assert_inside(saved: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_saved = saved.resolve()
    assert resolved_saved.is_relative_to(resolved_root), (
        f"saved path escaped workspace: saved={resolved_saved!s}, "
        f"workspace={resolved_root!s}"
    )


def test_save_upload_sanitises_dotdot_filename(tmp_path: Path) -> None:
    """A filename containing ``../`` must not escape ``dest_dir``."""
    from api.services.files import save_upload

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    saved = _run(save_upload(_FakeUpload("../../escape.txt"), workspace))

    _assert_inside(saved, workspace)
    assert "/" not in saved.name
    assert "\\" not in saved.name
    assert ".." not in saved.name.split("_")


def test_save_upload_sanitises_windows_separators(tmp_path: Path) -> None:
    """Backslashes (Windows path separator) must be stripped."""
    from api.services.files import save_upload

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    saved = _run(save_upload(_FakeUpload(r"..\..\evil.bin"), workspace))

    _assert_inside(saved, workspace)
    assert "\\" not in saved.name
    assert ".." not in saved.name.split("_")


def test_save_upload_absolute_filename_stays_inside(tmp_path: Path) -> None:
    """An absolute-looking filename must collapse to a clean basename."""
    from api.services.files import save_upload

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    saved = _run(save_upload(_FakeUpload("/etc/passwd_pwn"), workspace))

    _assert_inside(saved, workspace)
    assert saved.parent.resolve() == workspace.resolve()


def test_save_upload_resists_collision_traversal(tmp_path: Path) -> None:
    """The headline real-world exploit: workspace already contains
    ``<ts>_marker`` (from a previous upload, race, or attacker prep)
    and the next filename is ``marker/../../ESCAPED``. Without the fix
    this writes to ``<tmp>/ESCAPED`` — *outside* the workspace.
    """
    from api.services.files import save_upload

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Pre-create candidate prefix dirs for every second of the current minute,
    # guaranteeing that whichever ts ``save_upload`` picks will collide.
    now = datetime.now().replace(microsecond=0)
    base_min = now.strftime("%Y%m%d_%H%M")
    for sec in range(60):
        (workspace / f"{base_min}{sec:02d}_marker").mkdir(parents=True, exist_ok=True)

    sentinel = tmp_path / "ESCAPED"
    if sentinel.exists():
        sentinel.unlink()

    saved = _run(save_upload(_FakeUpload("marker/../../ESCAPED", b"OWNED"), workspace))

    _assert_inside(saved, workspace)
    assert not sentinel.exists(), (
        f"path traversal succeeded: sentinel was written at {sentinel}"
    )


def test_save_upload_blank_filename_falls_back_to_default(tmp_path: Path) -> None:
    """An empty / whitespace-only filename must fall back to a safe default
    instead of producing a dangling ``<ts>_`` name.
    """
    from api.services.files import save_upload

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    saved = _run(save_upload(_FakeUpload(""), workspace))

    _assert_inside(saved, workspace)
    assert not saved.name.endswith("_")


def test_save_upload_dot_filename_falls_back_to_default(tmp_path: Path) -> None:
    """``..`` as a basename (after stripping separators) is the canonical
    traversal payload; must be replaced by the default.
    """
    from api.services.files import save_upload

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    saved = _run(save_upload(_FakeUpload(".."), workspace))

    _assert_inside(saved, workspace)
    assert ".." not in saved.name.split("_")


def test_safe_filename_helper_unit() -> None:
    """Direct unit coverage of the sanitiser helper."""
    from api.services.files import _safe_filename

    assert _safe_filename("../../etc/cron.d/x") == "x"
    assert _safe_filename(r"..\..\evil.bin") == "evil.bin"
    assert _safe_filename("/etc/passwd") == "passwd"
    assert _safe_filename("..") == "upload"
    assert _safe_filename(".") == "upload"
    assert _safe_filename("...") == "upload"
    assert _safe_filename("") == "upload"
    assert _safe_filename(None) == "upload"
    # Non-alphanumeric (including spaces / semicolons) becomes ``_``.
    assert _safe_filename("a b;c d.txt") == "a_b_c_d.txt"
    # Length cap so a single component cannot blow up the FS limit.
    long = "a" * 5_000 + ".bin"
    assert len(_safe_filename(long)) <= 200
