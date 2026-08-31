#!/usr/bin/env python3
"""Enforce the canonical resolution replacement activation boundary."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

OWNER = Path("src/apm_cli/install/resolution_staging.py")
CONSUMER = Path("src/apm_cli/install/phases/resolve.py")
OWNER_METHODS = {"prepare_replacement", "publish_replacement", "discard_replacement"}


def _parse(root: Path, relative: Path) -> ast.Module:
    """Parse one repository-relative Python module."""
    return ast.parse((root / relative).read_text(encoding="utf-8"))


def _defined_functions(tree: ast.AST) -> list[str]:
    """Return every function name in a syntax tree."""
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _call_attributes(tree: ast.AST) -> list[ast.Attribute]:
    """Return attribute expressions used as call targets."""
    return [
        node.func
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def find_violations(root: Path) -> list[str]:
    """Return canonical replacement-boundary violations below *root*."""
    violations: list[str] = []
    owner_tree = _parse(root, OWNER)
    owner_defs = _defined_functions(owner_tree)
    missing = sorted(OWNER_METHODS - set(owner_defs))
    if missing:
        violations.append(f"owner is missing methods: {', '.join(missing)}")

    source_root = root / "src/apm_cli"
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(root)
        if relative == OWNER:
            continue
        duplicates = OWNER_METHODS.intersection(_defined_functions(_parse(root, relative)))
        if duplicates:
            violations.append(
                f"{relative.as_posix()} duplicates owner methods: {', '.join(sorted(duplicates))}"
            )

    consumer_tree = _parse(root, CONSUMER)
    consumer_calls = _call_attributes(consumer_tree)
    if not any(node.attr == "prepare_replacement" for node in consumer_calls):
        violations.append("resolve phase does not prepare replacements through the owner")
    if any(
        node.attr == "prepare_path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "staging_session"
        for node in consumer_calls
    ):
        violations.append("resolve phase eagerly removes a live path before replacement")
    activation_routes = [
        keyword.value
        for node in ast.walk(consumer_tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "activation_callback"
    ]
    routes_owner_directly = any(
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "staging_session"
        and value.attr == "publish_replacement"
        for value in activation_routes
    )
    routes_owner_through_acceptance = any(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "partial"
        and bool(value.args)
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "_activate_validated_candidate"
        for value in activation_routes
    ) and any(
        node.attr == "publish_replacement"
        and isinstance(node.value, ast.Name)
        and node.value.id == "staging_session"
        for node in consumer_calls
    )
    if not (routes_owner_directly or routes_owner_through_acceptance):
        violations.append("validated candidates do not publish through the staging owner")
    return violations


def main() -> int:
    """Run the boundary check from the repository root."""
    root = Path.cwd()
    violations = find_violations(root)
    if violations:
        for violation in violations:
            print(f"[x] {violation}")
        return 1
    print("[+] resolution replacement activation has one owner")
    return 0


if __name__ == "__main__":
    sys.exit(main())
