#!/usr/bin/env python3
"""Enforce the canonical bootstrap project-name authority."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE_ROOT = ROOT / "src" / "apm_cli"
OWNER = SOURCE_ROOT / "core" / "project_name.py"
RESOLVER = "resolve_bootstrap_project_name"
RESOLVER_NAMES = {RESOLVER, f"_{RESOLVER}"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_resolver_call(node: ast.AST, argument: str | None = None) -> bool:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id not in RESOLVER_NAMES
    ):
        return False
    if argument is None:
        return True
    return len(node.args) == 1 and _is_name(node.args[0], argument)


def _has_resolver_assignment(tree: ast.Module, target_name: str, argument: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(_is_name(target, target_name) for target in node.targets) and _is_resolver_call(
            node.value, argument
        ):
            return True
    return False


def _minimal_config_name(tree: ast.Module) -> ast.AST | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_create_minimal_config":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Dict):
                continue
            for key, value in zip(child.keys, child.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "name":
                    return value
    return None


def _has_named_assignment(tree: ast.Module, target_name: str, value_name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(_is_name(target, target_name) for target in node.targets) and _is_name(
            node.value, value_name
        ):
            return True
    return False


def _definitions(name: str) -> list[Path]:
    definitions = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if any(
            isinstance(node, ast.FunctionDef) and node.name == name
            for node in ast.walk(_tree(path))
        ):
            definitions.append(path)
    return definitions


def main() -> int:
    errors = []
    owner_tree = _tree(OWNER)
    constants = {
        target.id: node.value.value
        for node in owner_tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    if constants.get("DEFAULT_BOOTSTRAP_PROJECT_NAME") != "my-project":
        errors.append("canonical fallback constant is missing")

    for name in ("validate_project_name", RESOLVER):
        if _definitions(name) != [OWNER]:
            errors.append(f"{name} must be defined only by core/project_name.py")

    init_tree = _tree(SOURCE_ROOT / "commands" / "init.py")
    if not _has_resolver_assignment(init_tree, "final_project_name", "derived_project_name"):
        errors.append("init bootstrap must assign the resolver result")

    install_tree = _tree(SOURCE_ROOT / "commands" / "install.py")
    if not _has_resolver_assignment(install_tree, "project_name", "derived_project_name"):
        errors.append("install bootstrap must assign the resolver result")

    runner_value = _minimal_config_name(_tree(SOURCE_ROOT / "core" / "script_runner.py"))
    if not _is_resolver_call(runner_value):
        errors.append("ScriptRunner bootstrap name must be the resolver result")

    deps_tree = _tree(SOURCE_ROOT / "commands" / "deps" / "cli.py")
    if not _has_named_assignment(
        deps_tree, "project_name", "DEFAULT_BOOTSTRAP_PROJECT_NAME"
    ):
        errors.append("dependency tree fallback must use the canonical constant")

    if errors:
        for error in errors:
            print(f"[x] {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
