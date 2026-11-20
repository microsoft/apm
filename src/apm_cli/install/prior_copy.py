"""Keep a dependency's prior apm_modules copy until its replacement is verified (#4353).

Before an install re-downloads a package whose copy contradicts the lock, the
copy is moved to private scratch outside the project. A verified replacement
discards it; any refusal or failure restores it, so a failed install leaves
``apm_modules`` as it was.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any


def set_aside(ctx: Any, dep_key: str, path: Path) -> None:
    """Move *path* to private scratch; the package directory is then absent.

    A path that cannot be kept (already set aside this run, a symlink or not a
    directory) is removed, as the re-download replaces it anyway.
    """
    from apm_cli.config import get_apm_temp_dir
    from apm_cli.utils.path_security import safe_rmtree

    copies: dict[str, tuple[Path, Path]] = ctx.prior_copies
    if dep_key in copies or path.is_symlink() or not path.is_dir():
        if path.exists() or path.is_symlink():
            safe_rmtree(path, ctx.apm_modules_dir)
        return
    scratch = Path(tempfile.mkdtemp(prefix="apm-prior-copy-", dir=get_apm_temp_dir()))
    shutil.move(str(path), str(scratch / "copy"))
    copies[dep_key] = (scratch, path)


def restore(ctx: Any, dep_key: str) -> None:
    """Replace whatever this run fetched with the copy set aside, if any."""
    from apm_cli.utils.file_ops import robust_rmtree
    from apm_cli.utils.path_security import safe_rmtree

    entry = ctx.prior_copies.pop(dep_key, None)
    if entry is None:
        return
    scratch, path = entry
    if path.exists() or path.is_symlink():
        safe_rmtree(path, ctx.apm_modules_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(scratch / "copy"), str(path))
    robust_rmtree(scratch, ignore_errors=True)


def restore_all(ctx: Any) -> None:
    """Restore every copy still set aside; the install stops before verifying them."""
    for dep_key in list(ctx.prior_copies):
        restore(ctx, dep_key)


def discard(ctx: Any, dep_key: str) -> None:
    """Drop the copy set aside once its replacement passed verification."""
    from apm_cli.utils.file_ops import robust_rmtree

    entry = ctx.prior_copies.pop(dep_key, None)
    if entry is not None:
        robust_rmtree(entry[0], ignore_errors=True)
