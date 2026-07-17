#!/usr/bin/env python3
"""Enforce DeploymentReconciler ownership of generic-row contraction."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_OWNER = "DeploymentReconciler"


def _is_owner_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reconcile"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == _OWNER
    )


def _is_records_values_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "values"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "records"
    )


def _is_locator_field(node: ast.AST, field: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == field
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "locator"
    )


def _has_local_generic_algorithm(node: ast.AST) -> bool:
    return (
        any(_is_records_values_call(child) for child in ast.walk(node))
        and any(_is_locator_field(child, "target") for child in ast.walk(node))
        and any(_is_locator_field(child, "value") for child in ast.walk(node))
    )


def analyze_source(source: str) -> list[str]:
    """Return generic-row ownership boundary violations."""
    tree = ast.parse(source)
    violations: list[str] = []
    if not any(_is_owner_call(node) for node in ast.walk(tree)):
        violations.append(
            "shared target contraction must delegate to DeploymentReconciler.reconcile"
        )
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and _has_local_generic_algorithm(node):
            violations.append(
                f"line {node.lineno}: generic deployment row supersession belongs to "
                "DeploymentReconciler"
            )
    return violations


def analyze_path(path: Path) -> list[str]:
    """Read and analyze one manifest reconciliation consumer."""
    return analyze_source(path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    """Check the target-contraction consumer passed on the command line."""
    path = Path(argv[1]) if len(argv) > 1 else Path("src/apm_cli/install/manifest_reconcile.py")
    violations = analyze_path(path)
    if violations:
        for violation in violations:
            print(violation)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
