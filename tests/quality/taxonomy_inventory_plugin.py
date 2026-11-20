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
    "asyncio.subprocess": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
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


def _is_spawn_target(qualified: str) -> bool:
    module, _sep, attribute = qualified.rpartition(".")
    for allowed in SPAWN_SURFACES.get(module, ()):
        if allowed.endswith("*") and attribute.startswith(allowed[:-1]):
            return True
        if attribute == allowed:
            return True
    return False


def _bind_imports(node: ast.stmt, scope: dict[str, str]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                scope[alias.asname] = alias.name
            else:
                root = alias.name.split(".", 1)[0]
                scope[root] = root
    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        for alias in node.names:
            scope[alias.asname or alias.name] = f"{node.module}.{alias.name}"


def _qualified(func: ast.expr, scope: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name) or func.id not in scope:
        return None
    return ".".join([scope[func.id], *reversed(parts)])


def _scope_spawns(body: list[ast.stmt], outer: dict[str, str]) -> bool:
    """Check one lexical scope; imports inside a function stay in that function."""
    scope = dict(outer)
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in _walk_outside_scopes(statement):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                _bind_imports(node, scope)
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _scope_spawns(statement.body, scope):
                return True
            nested = [*statement.decorator_list]
            if not isinstance(statement, ast.ClassDef):
                nested += [*statement.args.defaults, *statement.args.kw_defaults]
            nodes = [child for item in nested if item is not None for child in ast.walk(item)]
        else:
            nodes = list(_walk_outside_scopes(statement))
        for node in nodes:
            if isinstance(node, ast.Call):
                qualified = _qualified(node.func, scope)
                if qualified is not None and _is_spawn_target(qualified):
                    return True
    return False


def _walk_outside_scopes(node: ast.AST):
    """Yield nodes under ``node`` without entering nested function or class bodies."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _walk_outside_scopes(child)


def spawns_processes(source: str) -> bool:
    """Return whether module source calls a process-spawning API directly."""
    return _scope_spawns(ast.parse(source).body, {})


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
