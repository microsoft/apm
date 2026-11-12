"""Shared JSON baseline lifecycle for bounded quality ratchets."""

from __future__ import annotations

import json
import shutil
import subprocess
from json import JSONDecodeError
from pathlib import Path

from apm_cli.utils.atomic_io import atomic_write_text

PROVISIONAL_KEYS = {"basis_commit", "required_follow_up"}


class BaselineError(ValueError):
    """Raised when a ratchet baseline cannot be trusted or updated."""


def load_baseline(path: Path, *, label: str) -> dict[str, object]:
    """Load one baseline as a JSON object with consistent errors."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (
        FileNotFoundError,
        IsADirectoryError,
        PermissionError,
        JSONDecodeError,
    ) as error:
        raise BaselineError(f"invalid {label} baseline {path}: {error}") from error
    if not isinstance(payload, dict):
        raise BaselineError(f"invalid {label} baseline: root must be an object")
    return payload


def load_baseline_at_ref(
    root: Path,
    path: Path,
    ref: str,
    *,
    label: str,
) -> dict[str, object] | None:
    """Load the committed baseline at a git ref, or None when absent there.

    Shrink-only ratchets compare the working baseline with the base branch
    copy so a change cannot grandfather new debt by editing the JSON.
    """
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise BaselineError(f"{label} baseline {path} is outside {root}") from error
    git = shutil.which("git")
    if git is None:
        raise BaselineError("git executable not found")
    verify = subprocess.run(  # noqa: S603 - fixed git operation, no shell
        [git, "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        raise BaselineError(f"unknown git ref for {label} baseline: {ref}")
    shown = subprocess.run(  # noqa: S603 - fixed git operation, no shell
        [git, "-C", str(root), "show", f"{ref}:{relative}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if shown.returncode != 0:
        return None
    try:
        payload = json.loads(shown.stdout)
    except JSONDecodeError as error:
        raise BaselineError(f"invalid {label} baseline at {ref}: {error}") from error
    if not isinstance(payload, dict):
        raise BaselineError(f"invalid {label} baseline at {ref}: root must be an object")
    return payload


def validate_provisional(
    payload: dict[str, object],
    path: Path,
    *,
    allow: bool,
    label: str,
) -> dict[str, str] | None:
    """Validate provisional metadata and enforce final mode."""
    provisional = payload.get("provisional")
    if provisional is not None and (
        not isinstance(provisional, dict)
        or set(provisional) != PROVISIONAL_KEYS
        or not all(isinstance(value, str) and value for value in provisional.values())
    ):
        raise BaselineError(f"invalid {label} baseline: malformed provisional metadata")
    if provisional is not None and not allow:
        raise BaselineError(
            f"provisional baseline is not allowed in final mode: {path}. "
            "Remeasure and remove provisional metadata before final validation; "
            "use --allow-provisional only for explicitly provisional checks."
        )
    return provisional


def write_baseline(
    path: Path,
    payload: dict[str, object],
    *,
    label: str,
) -> None:
    """Atomically write canonical sorted JSON.

    Routes through :func:`apm_cli.utils.atomic_io.atomic_write_text`, the
    single canonical atomic-write primitive, so the on-disk bytes use LF
    line endings on every platform (Windows text-mode writes would
    otherwise translate ``\\n`` to ``\\r\\n``, see microsoft/apm#2233).
    """
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, content)
    except OSError as error:
        raise BaselineError(f"failed to update {label} baseline: {error}") from error
