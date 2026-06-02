"""Cross-platform path utilities for APM CLI.

Centralises the resolve-then-relativise-then-posixify pattern so every
call site gets Windows-safe, forward-slash relative paths by default.
"""

from __future__ import annotations

from pathlib import Path


def portable_relpath(path: Path, base: Path) -> str:
    """Return a forward-slash relative path, resolving both sides first.

    Handles Windows 8.3 short names (e.g. ``RUNNER~1`` vs ``runneradmin``)
    and ensures consistent POSIX output on every platform.

    When *path* is not under *base* (or resolution fails), falls back to
    a resolved absolute POSIX path.
    """
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        try:
            return path.resolve().as_posix()
        except (OSError, RuntimeError):
            return path.as_posix()


def resolve_base_and_source_dirs(
    base_dir: str | Path, source_dir: str | Path | None
) -> tuple[Path, Path]:
    """Resolve a compiler's write-root and source-root into absolute Paths.

    ``base_dir`` is where outputs are written; ``source_dir`` is where
    primitives are read. ``source_dir`` defaults to ``base_dir`` for
    back-compat and only diverges under ``apm compile --root`` (writes
    redirected, sources stay in ``$PWD``). Resolution falls back to
    ``absolute()`` when the path does not yet exist on disk.
    """

    def _resolve(value: str | Path) -> Path:
        try:
            return Path(value).resolve()
        except (OSError, FileNotFoundError):
            return Path(value).absolute()

    resolved_base = _resolve(base_dir)
    resolved_source = resolved_base if source_dir is None else _resolve(source_dir)
    return resolved_base, resolved_source
