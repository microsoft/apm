"""Canonical tracked Python file inventory for test-quality ratchets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def tracked_python_paths(
    root: Path,
    *,
    scope: tuple[str, ...] = (),
) -> list[Path]:
    """Return contained, non-symlink tracked Python paths for `scope`."""
    root = root.resolve()
    git = shutil.which("git")
    if git is None:
        raise ValueError("git executable not found")
    pathspecs = [f":(glob){pattern}" for pattern in scope]
    result = subprocess.run(  # noqa: S603 - fixed git operation, no shell
        [git, "-C", str(root), "ls-files", "-z", "--", *pathspecs],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git ls-files failed: {detail}")

    paths: list[Path] = []
    for relative_text in result.stdout.decode("utf-8").split("\0"):
        if not relative_text or not relative_text.endswith(".py"):
            continue
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"tracked Python path escapes repository: {relative_text}")
        path = root / relative
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"tracked Python path resolves outside repository: {relative_text}"
            ) from error
        if path.is_symlink():
            raise ValueError(f"tracked Python path is a symlink: {relative_text}")
        if not path.is_file():
            raise ValueError(f"tracked Python path is not a regular file: {relative_text}")
        paths.append(path)
    return sorted(paths)


def is_test_module_path(path: str | Path) -> bool:
    """Return whether a path matches either pytest module naming convention."""
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py")
