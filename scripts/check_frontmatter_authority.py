#!/usr/bin/env python3
"""Enforce the canonical Markdown frontmatter detection boundary."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

OWNER = Path("src/apm_cli/utils/yaml_io.py")
PARSER_METHODS = {"load", "loads", "parse"}


def _frontmatter_aliases(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Return module aliases and imported parser-function aliases."""
    modules: set[str] = set()
    functions: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "frontmatter":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "frontmatter":
            for alias in node.names:
                if alias.name in PARSER_METHODS:
                    functions[alias.asname or alias.name] = alias.name
    return modules, functions


def _frontmatter_call_name(
    node: ast.Call,
    modules: set[str],
    functions: dict[str, str],
) -> str | None:
    """Return the frontmatter parser entry point called by node."""
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in modules
        and node.func.attr in PARSER_METHODS
    ):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return functions.get(node.func.id)
    return None


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


def _is_negated_handler_detect(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and isinstance(node.operand, ast.Call)
        and _is_handler_detect(node.operand)
    )


def _is_post_text_return(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "frontmatter"
        and node.value.func.attr == "Post"
        and len(node.value.args) == 1
        and isinstance(node.value.args[0], ast.Name)
        and node.value.args[0].id == "text"
    )


def _is_bounded_loads_return(
    node: ast.stmt,
    modules: set[str],
    functions: dict[str, str],
) -> bool:
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
        return False
    call = node.value
    if _frontmatter_call_name(call, modules, functions) != "loads":
        return False
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Name) or call.args[0].id != "text":
        return False
    handler = next((item.value for item in call.keywords if item.arg == "handler"), None)
    return isinstance(handler, ast.Name) and handler.id == "_BOUNDED_FRONTMATTER_HANDLER"


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
    owner_modules, owner_functions = _frontmatter_aliases(owner)
    parser_calls = [
        (node, _frontmatter_call_name(node, owner_modules, owner_functions))
        for node in ast.walk(owner)
        if isinstance(node, ast.Call)
    ]
    loads_calls = [node for node, name in parser_calls if name == "loads"]
    bypass_calls = [node for node, name in parser_calls if name in {"load", "parse"}]
    gates = [
        (index, node)
        for index, node in enumerate(owner.body)
        if isinstance(node, ast.If)
        and _is_negated_handler_detect(node.test)
        and len(node.body) == 1
        and _is_post_text_return(node.body[0])
        and not node.orelse
    ]
    bounded_returns = [
        (index, node)
        for index, node in enumerate(owner.body)
        if _is_bounded_loads_return(node, owner_modules, owner_functions)
    ]
    valid_control_flow = (
        len(gates) == 1
        and len(bounded_returns) == 1
        and gates[0][0] < bounded_returns[0][0]
        and len(loads_calls) == 1
    )
    if not valid_control_flow:
        violations.append(
            "load_frontmatter must gate one bounded frontmatter.loads return "
            "behind a negated bounded-handler detect return"
        )
    if bypass_calls:
        violations.append("load_frontmatter must not bypass its line-1 gate with load or parse")

    source_root = root / "src/apm_cli"
    for path in sorted(source_root.rglob("*.py")):
        if path == owner_path:
            continue
        source = path.read_text(encoding="utf-8")
        if "frontmatter" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        modules, functions = _frontmatter_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _frontmatter_call_name(node, modules, functions) in PARSER_METHODS:
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
