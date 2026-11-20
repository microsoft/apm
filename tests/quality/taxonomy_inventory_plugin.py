"""Emit one repository-wide module/node behavioral-marker inventory."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

BEHAVIORAL_MARKERS = frozenset({"unit", "component", "e2e"})
PROMISE_MARKER = "promise"
OUTPUT_ENV = "APM_TAXONOMY_INVENTORY"
# Process-spawning call surfaces: module -> attribute names (or prefixes ending in "*").
SPAWN_SURFACES: dict[str, frozenset[str]] = {
    "subprocess": frozenset(
        {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    ),
    "os": frozenset({"system", "popen", "fork", "forkpty", "exec*", "spawn*", "posix_spawn*"}),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
    "pexpect": frozenset({"spawn", "run"}),
}


def _module_marks(item: pytest.Item) -> list[object]:
    """Return the marks declared by the collected item's module-level pytestmark."""
    module = getattr(item, "module", None)
    raw_markers = getattr(module, "pytestmark", ())
    if not isinstance(raw_markers, (list, tuple)):
        raw_markers = (raw_markers,)
    return [getattr(marker, "mark", marker) for marker in raw_markers]


def _module_behavioral_markers(item: pytest.Item) -> list[str]:
    """Return behavioral marker names declared by the collected item's module."""
    return sorted(
        name
        for mark in _module_marks(item)
        if (name := getattr(mark, "name", None)) in BEHAVIORAL_MARKERS
    )


def _module_promises(item: pytest.Item) -> list[str]:
    """Return promise ids named by the module-level ``pytest.mark.promise`` marks."""
    return sorted(
        {
            str(arg)
            for mark in _module_marks(item)
            if getattr(mark, "name", None) == PROMISE_MARKER
            for arg in getattr(mark, "args", ())
        }
    )


def _is_spawn_attribute(module: str, attribute: str) -> bool:
    for allowed in SPAWN_SURFACES.get(module, ()):
        if allowed.endswith("*") and attribute.startswith(allowed[:-1]):
            return True
        if attribute == allowed:
            return True
    return False


def spawns_processes(source: str) -> bool:
    """Return whether module source calls a process-spawning API directly."""
    tree = ast.parse(source)
    module_aliases: dict[str, str] = {}
    name_aliases: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in SPAWN_SURFACES:
                    module_aliases[alias.asname or root] = root
        elif isinstance(node, ast.ImportFrom) and node.module in SPAWN_SURFACES:
            for alias in node.names:
                name_aliases[alias.asname or alias.name] = (node.module, alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
            and _is_spawn_attribute(module_aliases[func.value.id], func.attr)
        ):
            return True
        if (
            isinstance(func, ast.Name)
            and func.id in name_aliases
            and _is_spawn_attribute(*name_aliases[func.id])
        ):
            return True
    return False


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
    module_items = sorted({item.nodeid.split("::", 1)[0]: item for item in session.items}.items())
    modules = {module_path: _module_behavioral_markers(item) for module_path, item in module_items}
    promises = {module_path: _module_promises(item) for module_path, item in module_items}
    spawning = [
        module_path
        for module_path, item in module_items
        if spawns_processes(Path(item.path).read_text(encoding="utf-8"))
    ]
    Path(output).write_text(
        json.dumps(
            {
                "modules": modules,
                "nodes": nodes,
                "promises": promises,
                "spawning": spawning,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
