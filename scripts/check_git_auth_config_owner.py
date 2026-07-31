#!/usr/bin/env python3
"""Enforce one owner for indexed Git auth-config retain/reindex policy."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

OWNER_MODULE = "apm_cli.utils.git_env"
OWNER_SYMBOL = "strip_git_auth_config_entries"
OWNER_PATH = Path("src/apm_cli/utils/git_env.py")
CONSUMERS = {
    Path("src/apm_cli/core/auth.py"): "AuthResolver._clear_git_auth_env",
    Path("src/apm_cli/utils/github_host.py"): "set_authorization_header_git_env",
}
EXEMPT_MARKER = "architecture-authority-exempt:"


def _qualnamed_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return all functions keyed by class-qualified name."""
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, (*prefix, child.name))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, child.name))
                functions[qualname] = child
                visit(child, (*prefix, child.name))
            else:
                visit(child, prefix)

    visit(tree, ())
    return functions


def _imports_owner(tree: ast.Module) -> bool:
    """Return whether a module imports the canonical helper directly."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == OWNER_MODULE
        and any(alias.name == OWNER_SYMBOL and alias.asname is None for alias in node.names)
        for node in tree.body
    )


def _owner_call_count(node: ast.AST) -> int:
    """Count direct calls to the canonical helper."""
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == OWNER_SYMBOL
    )


def _inline_predicate_lines(tree: ast.AST) -> list[int]:
    """Find exact auth-classifier literals used by comparison/search syntax."""
    classifier_literals = {"extraheader", "authorization", "authorization:"}
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(
                isinstance(operand, ast.Constant)
                and isinstance(operand.value, str)
                and operand.value.casefold() in classifier_literals
                for operand in operands
            ):
                lines.add(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"__contains__", "count", "endswith", "find", "index", "startswith"}
            and any(
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and argument.value.casefold() in classifier_literals
                for argument in node.args
            )
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _references_indexed_git_config(node: ast.AST) -> bool:
    """Return whether a function touches the indexed GIT_CONFIG_* contract."""
    prefixes = ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.startswith(prefixes)
        for child in ast.walk(node)
    )


def analyze(root: Path) -> list[str]:
    """Return architecture violations under *root*."""
    violations: list[str] = []
    owner = root / OWNER_PATH
    owner_tree = ast.parse(owner.read_text(encoding="utf-8"), filename=str(owner))
    owner_definitions = [
        node
        for node in owner_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == OWNER_SYMBOL
    ]
    if len(owner_definitions) != 1:
        violations.append(f"{OWNER_PATH}: expected exactly one top-level {OWNER_SYMBOL} definition")

    for relative_path, function_name in CONSUMERS.items():
        path = root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = _qualnamed_functions(tree)
        function = functions.get(function_name)
        if not _imports_owner(tree):
            violations.append(f"{relative_path}: must import {OWNER_SYMBOL} from {OWNER_MODULE}")
        if function is None:
            violations.append(f"{relative_path}: required consumer {function_name} is missing")
        elif _owner_call_count(function) != 1:
            violations.append(
                f"{relative_path}: {function_name} must call {OWNER_SYMBOL} exactly once"
            )

    source_root = root / "src/apm_cli"
    for path in source_root.rglob("*.py"):
        relative_path = path.relative_to(root)
        if relative_path == OWNER_PATH:
            continue
        source = path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        duplicate_lines = {
            line
            for function in _qualnamed_functions(tree).values()
            if _references_indexed_git_config(function)
            for line in _inline_predicate_lines(function)
            if EXEMPT_MARKER not in source_lines[line - 1]
        }
        for line in sorted(duplicate_lines):
            violations.append(
                f"{relative_path}:{line}: indexed Git auth-config classification "
                f"must route through {OWNER_SYMBOL}"
            )
    return violations


def main() -> int:
    """Run the owner-boundary checker."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    violations = analyze(args.root.resolve())
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
