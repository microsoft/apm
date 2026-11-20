"""Emit one repository-wide module/node behavioral-marker inventory."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

BEHAVIORAL_MARKERS = frozenset({"unit", "component", "e2e"})
PROMISE_MARKER = "promise"
OUTPUT_ENV = "APM_TAXONOMY_INVENTORY"


def _module_markers(item: pytest.Item) -> list[pytest.Mark]:
    """Return the marks declared by the collected item's module-level pytestmark."""
    module = getattr(item, "module", None)
    raw_markers = getattr(module, "pytestmark", ())
    if not isinstance(raw_markers, (list, tuple)):
        raw_markers = (raw_markers,)
    return [getattr(marker, "mark", marker) for marker in raw_markers]


def _module_behavioral_markers(item: pytest.Item) -> list[str]:
    """Return behavioral marker names declared by the collected item's module."""
    names = [
        name
        for marker in _module_markers(item)
        if (name := getattr(marker, "name", None)) in BEHAVIORAL_MARKERS
    ]
    return sorted(names)


def _module_promises(item: pytest.Item) -> list[str]:
    """Return promise ids declared by the module; malformed ids stay visible."""
    ids: list[str] = []
    for marker in _module_markers(item):
        if getattr(marker, "name", None) != PROMISE_MARKER:
            continue
        ids.extend(
            arg if isinstance(arg, str) else f"<invalid {arg!r}>"
            for arg in getattr(marker, "args", ())
        )
        if getattr(marker, "kwargs", None):
            ids.append(f"<invalid kwargs {sorted(marker.kwargs)!r}>")
    return ids


def pytest_collection_finish(session: pytest.Session) -> None:
    """Write deterministic module declarations and effective node markers."""
    output = os.environ.get(OUTPUT_ENV)
    if output is None:
        raise pytest.UsageError(f"{OUTPUT_ENV} must name the inventory output")
    nodes = {
        item.nodeid: sorted(
            [marker.name for marker in item.iter_markers() if marker.name in BEHAVIORAL_MARKERS]
        )
        for item in session.items
    }
    first_items = {item.nodeid.split("::", 1)[0]: item for item in session.items}
    modules = {
        module_path: _module_behavioral_markers(item)
        for module_path, item in sorted(first_items.items())
    }
    promises = {
        module_path: ids
        for module_path, item in sorted(first_items.items())
        if (ids := _module_promises(item))
    }
    Path(output).write_text(
        json.dumps(
            {
                "modules": modules,
                "nodes": nodes,
                "promises": promises,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
