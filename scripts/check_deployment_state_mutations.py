#!/usr/bin/env python3
"""Reject deployment-state mutation outside its canonical codec owners."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

_OWNED_FIELDS = frozenset(
    {
        "deployed_files",
        "deployed_file_hashes",
        "local_deployed_files",
        "local_deployed_file_hashes",
        "mcp_target_servers",
    }
)
_MUTATORS = frozenset({"append", "remove", "pop", "extend", "clear", "update", "insert"})
# This bounded AST guard intentionally does not perform alias dataflow analysis.
# A pattern such as ``view = lock.mcp_target_servers; view.clear()`` requires a
# heavier analyzer and remains covered by review plus the behavioral trap.
_ALLOWED = frozenset(
    {
        Path("core/deployment_state.py"),
        Path("core/deployment_ledger.py"),
        Path("deps/lockfile.py"),
    }
)


def _owned_attribute(node: ast.AST) -> bool:
    """Return whether one AST node references codec-owned state."""
    return isinstance(node, ast.Attribute) and node.attr in _OWNED_FIELDS


def mutation_lines(source: str) -> list[int]:
    """Return direct mutation lines for codec-owned state in Python source."""
    tree = ast.parse(source)
    violations: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if _owned_attribute(target) or (
                    isinstance(target, ast.Subscript) and _owned_attribute(target.value)
                ):
                    violations.add(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
            and _owned_attribute(node.func.value)
        ):
            violations.add(node.lineno)
    return sorted(violations)


def analyze_tree(root: Path) -> list[str]:
    """Return repository-relative violations below one apm_cli source root."""
    violations: list[str] = []
    for source in sorted(root.rglob("*.py")):
        relative = source.relative_to(root)
        if relative in _ALLOWED:
            continue
        for line_number in mutation_lines(source.read_text(encoding="utf-8")):
            violations.append(f"{relative.as_posix()}:{line_number}")
    return violations


def main() -> int:
    """Run the deployment-state mutation boundary check."""
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    violations = analyze_tree(args.root)
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
