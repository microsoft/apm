#!/usr/bin/env python3
"""Guard canonical LF writers for generated files inside hashed package trees."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WriterContract:
    """One function and the canonical helper it must call exactly once."""

    path: str
    function: str
    helper: str


CONTRACTS = (
    WriterContract(
        "src/apm_cli/deps/plugin_parser.py",
        "synthesize_apm_yml_from_plugin",
        "write_text_lf",
    ),
    WriterContract(
        "src/apm_cli/deps/plugin_parser.py",
        "_map_plugin_artifacts",
        "write_text_lf",
    ),
    WriterContract(
        "src/apm_cli/utils/yaml_io.py",
        "dump_yaml",
        "write_text_lf",
    ),
)
DIRECT_WRITERS = {"open", "write_bytes", "write_text"}


def _call_name(node: ast.Call) -> str | None:
    """Return the terminal name of a direct or attribute call."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find one top-level function by name."""
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def check_contract(root: Path, contract: WriterContract) -> list[str]:
    """Return violations for one hash-visible writer contract."""
    source_path = root / contract.path
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=contract.path)
    except (OSError, SyntaxError) as exc:
        return [f"{contract.path}: cannot inspect source: {exc}"]

    function = _find_function(tree, contract.function)
    if function is None:
        return [f"{contract.path}: expected exactly one {contract.function} definition"]

    calls = [_call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)]
    violations: list[str] = []
    if calls.count(contract.helper) != 1:
        violations.append(
            f"{contract.path}:{contract.function} must call {contract.helper} exactly once"
        )
    bypasses = sorted(name for name in calls if name in DIRECT_WRITERS)
    if bypasses:
        violations.append(
            f"{contract.path}:{contract.function} bypasses canonical LF writer via "
            + ", ".join(bypasses)
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    """Check every guarded hash-visible writer."""
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else Path.cwd()
    violations = [
        violation for contract in CONTRACTS for violation in check_contract(root, contract)
    ]
    if violations:
        for violation in violations:
            print(f"[x] {violation}")
        return 1
    print("[+] hash-visible LF writer boundaries clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
