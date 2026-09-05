"""Refusal guard for durable artifacts that still name a staging path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

STAGING_DIR_NAME = ".apm-resolution-staging"

_EXCERPT_MARGIN = 120
_ROOT_LOCATION = "its serialized content"


class StagingPathLeakError(RuntimeError):
    """A durable artifact still points inside a resolution staging root."""


def assert_no_staging_paths(payload: Any, artifact: str) -> None:
    """Refuse to persist *payload* while it references a staging path."""
    offender = _find_staging_reference(payload, "")
    if offender is None:
        return
    location, text = offender
    raise StagingPathLeakError(
        f"Refusing to write {artifact}: {location or _ROOT_LOCATION} references the "
        f"resolution staging directory, which is removed once the install finishes, "
        f"so the recorded path would never resolve again ({_excerpt(text)}). "
        f"Re-run the install."
    )


def _find_staging_reference(payload: Any, location: str) -> tuple[str, str] | None:
    """Return the location and text of the first value naming a staging directory."""
    if isinstance(payload, (str, Path)):
        text = str(payload)
        return (location, text) if STAGING_DIR_NAME in text else None
    for candidate_location, candidate in _child_values(payload, location):
        found = _find_staging_reference(candidate, candidate_location)
        if found is not None:
            return found
    return None


def _child_values(payload: Any, location: str) -> list[tuple[str, Any]]:
    """Return the (location, value) pairs to search below *payload*."""
    if isinstance(payload, dict):
        children: list[tuple[str, Any]] = []
        for key, value in payload.items():
            children.append((location, key))
            children.append((_key_location(location, key), value))
        return children
    if isinstance(payload, (set, frozenset)):
        # Sort so a failure reports the same member on every run.
        return [(location, item) for item in sorted(payload, key=repr)]
    if isinstance(payload, (list, tuple)):
        return [(f"{location}[{index}]", item) for index, item in enumerate(payload)]
    return []


def _key_location(location: str, key: Any) -> str:
    """Return the dotted location of *key* below *location*."""
    name = key if isinstance(key, str) else repr(key)
    return f"{location}.{name}" if location else name


def _excerpt(text: str) -> str:
    """Return a bounded window around the staging marker for error messages."""
    index = text.find(STAGING_DIR_NAME)
    start = max(0, index - _EXCERPT_MARGIN)
    end = min(len(text), index + len(STAGING_DIR_NAME) + _EXCERPT_MARGIN)
    return text[start:end].strip()
