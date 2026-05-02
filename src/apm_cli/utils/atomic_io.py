"""Atomic file-write primitive for APM.

Writes go to a temp file in the same directory as the target, then are
renamed via :func:`os.replace`. A crash mid-write cannot leave a half-
written destination, and on POSIX the rename is atomic with respect to
concurrent readers.

This is the single canonical implementation; both
``apm_cli.commands._helpers._atomic_write`` (kept as an alias for
backward compatibility with existing tests) and
``apm_cli.compilation.output_writer`` route through here.
"""

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, data: str, *, new_file_mode: int | None = None) -> None:
    """Atomically write ``data`` (UTF-8) to ``path``.

    The temp file is created in ``path.parent`` so the eventual
    ``os.replace`` is a same-filesystem rename. Caller is responsible
    for ensuring the parent directory exists.

    If ``new_file_mode`` is given and ``path`` does not yet exist,
    the temp file's POSIX mode bits are set to that value before
    the rename so the destination is created with the requested
    permissions. Existing files keep their pre-existing mode (we
    do not downgrade nor upgrade perms). The mode hint is silently
    ignored on platforms where ``os.fchmod`` is unavailable
    (e.g. Windows), where POSIX mode bits are not enforced anyway.

    On any failure, the temp file is removed and the original target
    file (if any) remains untouched.
    """
    existed = path.exists()
    fd, tmp_name = tempfile.mkstemp(prefix="apm-atomic-", dir=str(path.parent))
    try:
        if new_file_mode is not None and not existed and hasattr(os, "fchmod"):
            with contextlib.suppress(OSError):
                os.fchmod(fd, new_file_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:  # noqa: SIM105
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
