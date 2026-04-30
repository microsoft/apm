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

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, data: str) -> None:
    """Atomically write ``data`` (UTF-8) to ``path``.

    The temp file is created in ``path.parent`` so the eventual
    ``os.replace`` is a same-filesystem rename. Caller is responsible
    for ensuring the parent directory exists.

    On any failure, the temp file is removed and the original target
    file (if any) remains untouched.
    """
    fd, tmp_name = tempfile.mkstemp(prefix="apm-atomic-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:  # noqa: SIM105
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
