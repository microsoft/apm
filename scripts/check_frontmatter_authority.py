#!/usr/bin/env python3
"""Enforce the canonical Markdown frontmatter detection boundary."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

OWNER = Path("src/apm_cli/utils/yaml_io.py")


def _is_frontmatter_call(node: ast.Call, name: str) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "frontmatter"
        and node.func.attr == name
    )


def _is_handler_detect(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_BOUNDED_FRONTMATTER_HANDLER"
        and node.func.attr == "detect"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "text"
    )


def check(root: Path) -> list[str]:
    """Return frontmatter authority violations under root."""
    owner_path = root / OWNER
    if not owner_path.is_file():
        return [f"missing canonical frontmatter owner: {OWNER.as_posix()}"]

    owner_tree = ast.parse(owner_path.read_text(encoding="utf-8"), filename=str(OWNER))
    owner = next(
        (
            node
            for node in owner_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "load_frontmatter"
        ),
        None,
    )
    if owner is None:
        return ["missing canonical load_frontmatter function"]

    violations: list[str] = []
    detect_calls = [
        node for node in ast.walk(owner) if isinstance(node, ast.Call) and _is_handler_detect(node)
    ]
    loads_calls = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and _is_frontmatter_call(node, "loads")
    ]
    direct_load_calls = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and _is_frontmatter_call(node, "load")
    ]
    if len(detect_calls) != 1:
        violations.append(
            "load_frontmatter must delegate fence detection to the bounded handler exactly once"
        )
    if len(loads_calls) != 1:
        violations.append(
            "load_frontmatter must route fenced text through bounded frontmatter.loads"
        )
    if direct_load_calls:
        violations.append("load_frontmatter must not bypass its line-1 gate with frontmatter.load")

    source_root = root / "src/apm_cli"
    for path in sorted(source_root.rglob("*.py")):
        if path == owner_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_frontmatter_call(node, "load") or _is_frontmatter_call(node, "loads"):
                relative = path.relative_to(root).as_posix()
                violations.append(
                    f"{relative}:{node.lineno}: direct frontmatter parsing must route through load_frontmatter"
                )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    violations = check(args.root.resolve())
    for violation in violations:
        print(f"[x] {violation}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
