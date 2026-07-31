#!/usr/bin/env python3
"""Enforce the canonical manifest dependency selection boundary."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

OWNER = Path("src/apm_cli/models/dependency/selection.py")
CONSUMER = Path("src/apm_cli/commands/uninstall/engine.py")
OWNED_DEFINITIONS = frozenset({"parse_dependency_entry", "select_manifest_dependency"})


def _parse(path: Path) -> ast.Module:
    """Parse one configured source path, failing closed on missing files."""
    if not path.is_file():
        raise RuntimeError(f"{path}: configured source path is missing")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return exactly one top-level function named *name*."""
    matches = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{CONSUMER}: expected exactly one {name}() definition")
    return matches[0]


def _iterated_names(node: ast.AST) -> set[str]:
    """Return every simple name referenced by one loop iterator expression."""
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _call_name(call: ast.Call) -> str | None:
    """Return the terminal name of one call expression."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _definition_violations(source_root: Path) -> list[str]:
    """Find owned function definitions outside the canonical module."""
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name in OWNED_DEFINITIONS and path != OWNER:
                violations.append(f"{path}:{node.lineno}: {node.name}() belongs in {OWNER}")
    return violations


def _validator_violations(validator: ast.FunctionDef) -> list[str]:
    """Reject selection logic reimplemented beside the canonical call."""
    violations: list[str] = []
    canonical_calls = [
        node
        for node in ast.walk(validator)
        if isinstance(node, ast.Call) and _call_name(node) == "select_manifest_dependency"
    ]
    if len(canonical_calls) != 1:
        violations.append(
            f"{CONSUMER}:{validator.lineno}: _validate_uninstall_packages() must call "
            "select_manifest_dependency() exactly once"
        )

    dependency_names = {"current_deps"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(validator):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            source_names = _iterated_names(node.value)
            if source_names.intersection(dependency_names) and target.id not in dependency_names:
                dependency_names.add(target.id)
                changed = True

    loops: Iterable[ast.AST] = (
        node
        for node in ast.walk(validator)
        if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension)
    )
    for loop in loops:
        iterator = loop.iter
        if _iterated_names(iterator).intersection(dependency_names):
            violations.append(
                f"{CONSUMER}:{loop.lineno}: manifest dependencies must be selected by "
                f"{OWNER}, not iterated in _validate_uninstall_packages()"
            )

    forbidden_calls = {"get_identity", "get_unique_key", "parse", "parse_from_dict"}
    for node in ast.walk(validator):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in forbidden_calls:
            violations.append(
                f"{CONSUMER}:{node.lineno}: identity parsing/matching in "
                f"_validate_uninstall_packages() must route through {OWNER}"
            )
    return violations


def check() -> list[str]:
    """Return every canonical-owner boundary violation."""
    owner_tree = _parse(OWNER)
    owner_defs = {
        node.name
        for node in owner_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    violations = []
    missing = sorted(OWNED_DEFINITIONS - owner_defs)
    if missing:
        violations.append(f"{OWNER}: missing owned definitions: {', '.join(missing)}")
    violations.extend(_definition_violations(Path("src/apm_cli")))
    consumer_tree = _parse(CONSUMER)
    violations.extend(
        _validator_violations(_function(consumer_tree, "_validate_uninstall_packages"))
    )
    return violations


def main() -> int:
    """Print violations and return a shell-friendly status."""
    try:
        violations = check()
    except (OSError, RuntimeError, SyntaxError) as exc:
        print(f"[x] uninstall selection owner check failed: {exc}", file=sys.stderr)
        return 1
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
