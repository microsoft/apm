#!/usr/bin/env python3
"""Guard the canonical attribution boolean passed to the AGENTS.md renderer."""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Sequence
from pathlib import Path

_COMPILER_CLASS = "DistributedAgentsCompiler"
_COMPILE_METHOD = "compile_distributed"
_RENDER_METHOD = "_generate_agents_content"
_CONFIG_KEY = "source_attribution"


def _find_method(tree: ast.Module, method_name: str) -> ast.FunctionDef | None:
    """Return ``method_name`` from the distributed compiler class."""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != _COMPILER_CLASS:
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == method_name:
                return member
    return None


def _reads_attribution_from_config(method: ast.FunctionDef) -> bool:
    """Return whether the method binds the canonical manifest boolean."""
    for node in ast.walk(method):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == _CONFIG_KEY
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "get"
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "config"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ):
            continue
        if node.value.args[0].value == _CONFIG_KEY:
            return True
    return False


def _forwards_attribution_to_renderer(method: ast.FunctionDef) -> bool:
    """Return whether the renderer receives the canonical boolean unchanged."""
    for node in ast.walk(method):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == _RENDER_METHOD
        ):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == _CONFIG_KEY
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == _CONFIG_KEY
            ):
                return True
    return False


def find_violations(path: Path) -> list[str]:
    """Return authority violations for the configured distributed compiler."""
    if not path.is_file():
        return [f"{path}: configured distributed compiler is missing or not a regular file"]

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{path}: cannot parse distributed compiler: {exc.msg}"]

    compile_method = _find_method(tree, _COMPILE_METHOD)
    if compile_method is None:
        return [f"{path}: {_COMPILER_CLASS}.{_COMPILE_METHOD} is missing"]

    violations: list[str] = []
    if not _reads_attribution_from_config(compile_method):
        violations.append(
            f"{path}: {_COMPILE_METHOD} must read {_CONFIG_KEY} from config before rendering"
        )
    if not _forwards_attribution_to_renderer(compile_method):
        violations.append(
            f"{path}: {_COMPILE_METHOD} must pass {_CONFIG_KEY}={_CONFIG_KEY} to "
            f"{_RENDER_METHOD}, not the placement source map"
        )
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Check that AGENTS.md cosmetics consume the manifest boolean authority."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Distributed compiler source file to inspect.")
    args = parser.parse_args(argv)

    violations = find_violations(args.path)
    if violations:
        for violation in violations:
            print(f"[x] {violation}")
        return 1

    print("[+] AGENTS.md source attribution uses the canonical config boolean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
