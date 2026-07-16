"""Shared JSON baseline lifecycle for bounded test-quality ratchets."""

from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path

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
    """Atomically write canonical sorted JSON."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        temporary.write_bytes(content.encode("utf-8"))
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise BaselineError(f"failed to update {label} baseline: {error}") from error
