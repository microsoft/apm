#!/usr/bin/env python3
"""Enforce DeploymentReconciler ownership of cleanup claim aggregation."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_OWNER_METHOD = "current_claimed_paths"
_CLAIMS_NAME = "package_deployed_files"
_AGGREGATION_METHODS = frozenset({"add", "update", "union"})


def _is_owner_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "DeploymentReconciler"
        and node.func.attr == _OWNER_METHOD
    )


def _is_claim_collection_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == _CLAIMS_NAME
        and node.func.attr in {"items", "values"}
    )


def _contains_claim_reference(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == _CLAIMS_NAME
        for child in ast.walk(node)
    )


def _loop_aggregates_claims(node: ast.For) -> bool:
    if not _is_claim_collection_call(node.iter):
        return False
    if isinstance(node.iter.func, ast.Attribute) and node.iter.func.attr == "values":
        return True
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in _AGGREGATION_METHODS
        for statement in node.body
        for child in ast.walk(statement)
    )


def analyze_source(source: str) -> list[str]:
    """Return semantic cleanup claim-authority violations."""
    tree = ast.parse(source)
    owner_calls = [node for node in ast.walk(tree) if _is_owner_call(node)]
    violations: list[str] = []
    if len(owner_calls) != 2:
        violations.append(
            "cleanup must delegate both current-claim decisions to "
            "DeploymentReconciler.current_claimed_paths"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.For) and _loop_aggregates_claims(node):
            violations.append(
                f"line {node.lineno}: cleanup must not aggregate "
                "package_deployed_files in a local loop"
            )
        if isinstance(node, (ast.SetComp, ast.ListComp, ast.GeneratorExp)) and (
            _contains_claim_reference(node)
        ):
            violations.append(
                f"line {node.lineno}: cleanup must not aggregate "
                "package_deployed_files in a local comprehension"
            )
    return violations


def analyze_path(path: Path) -> list[str]:
    """Read and analyze one cleanup module."""
    return analyze_source(path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(
        "src/apm_cli/install/phases/cleanup.py"
    )
    violations = analyze_path(path)
    if violations:
        for violation in violations:
            print(violation)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
