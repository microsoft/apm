#!/usr/bin/env python3
"""Detect dependency-target gates around per-file hook routing."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

DEFAULT_PATHS = (
    Path("src/apm_cli/integration/hook_integrator.py"),
    Path("src/apm_cli/integration/kiro_hook_integrator.py"),
)


def _references_dep_targets_active(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == "dep_targets_active" for child in ast.walk(node)
    )


def _calls_hook_file_filter(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == "_filter_hook_files_for_target":
            return True
    return False


def _violations_for_path(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not _references_dep_targets_active(node.test):
            continue
        if not _calls_hook_file_filter(node):
            continue
        line = lines[node.lineno - 1]
        if "architecture-authority-exempt:" in line:
            continue
        violations.append(
            f"{path}:{node.lineno}: dep_targets_active gates _filter_hook_files_for_target"
        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.paths or list(DEFAULT_PATHS)
    violations: list[str] = []
    for path in paths:
        violations.extend(_violations_for_path(path))
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
