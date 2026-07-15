#!/usr/bin/env python3
"""Enforce one printable-ASCII normalizer for agent diagnostic identifiers.

``apm_cli.utils.diagnostics.printable_ascii_text`` owns the normalization
applied to untrusted package and agent names before they reach diagnostic
output. The two consumers covered by this boundary must delegate directly:

* ``AgentIntegrator`` Codex diagnostic rendering; and
* OpenCode agent frontmatter validation.

This checker is intentionally narrow. It does not inspect hook event-name or
SkillSpector output sanitizers because those normalize different values under
different replacement semantics.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

OWNER_MODULE = "apm_cli.utils.diagnostics"
OWNER_SYMBOL = "printable_ascii_text"
OWNER_PATH = Path("src/apm_cli/utils/diagnostics.py")
CONSUMER_FUNCTIONS = {
    Path("src/apm_cli/integration/agent_integrator.py"): {
        "AgentIntegrator._warn_codex_unverified_scope": 2,
        "AgentIntegrator._warn_codex_tools_dropped": 2,
    },
    Path("src/apm_cli/integration/opencode_frontmatter.py"): {
        "validate_opencode_frontmatter": 2,
    },
}
RETIRED_SYMBOL = "_ascii_safe_name"


@dataclass(frozen=True)
class Violation:
    """One architecture-boundary violation."""

    path: Path
    line: int
    message: str

    def render(self) -> str:
        """Return an actionable one-line diagnostic."""
        return f"{self.path}:{self.line}: {self.message}"


def _qualnamed_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every function keyed by its class-qualified name."""
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, (*prefix, child.name))
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = ".".join((*prefix, child.name))
                functions[qualname] = child
                visit(child, (*prefix, child.name))
            else:
                visit(child, prefix)

    visit(tree, ())
    return functions


def _walk_own_scope(node: ast.AST) -> list[ast.AST]:
    """Return descendants without entering nested function scopes."""
    descendants: list[ast.AST] = []

    def visit(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            descendants.append(child)
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            visit(child)

    visit(node)
    return descendants


def _imports_owner(tree: ast.Module) -> bool:
    """Return whether the consumer imports the canonical owner directly."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == OWNER_MODULE
        and any(alias.name == OWNER_SYMBOL and alias.asname is None for alias in node.names)
        for node in tree.body
    )


def _owner_call_count(function: ast.AST) -> int:
    """Count direct calls to the canonical owner in one function."""
    return sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == OWNER_SYMBOL
        for node in _walk_own_scope(function)
    )


def _ascii_codec_call(node: ast.AST) -> bool:
    """Return whether a call locally encodes or decodes as ASCII."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"encode", "decode"}
        and node.args
    ):
        return False
    encoding = node.args[0]
    return (
        isinstance(encoding, ast.Constant)
        and isinstance(encoding.value, str)
        and encoding.value.lower() == "ascii"
    )


def _local_ascii_signal(node: ast.AST) -> bool:
    """Return whether a consumer locally implements printable-ASCII logic."""
    if _ascii_codec_call(node):
        return True
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "ord":
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "isascii",
            "isprintable",
        }:
            return True
    return isinstance(node, ast.Constant) and node.value in {0x20, 0x7E, 0x7F}


def _parse(path: Path) -> ast.Module:
    """Parse one configured Python path, failing closed."""
    if not path.is_file():
        raise FileNotFoundError(f"configured path is missing or not a file: {path}")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def check(root: Path) -> list[Violation]:
    """Return canonical-owner and consumer-routing violations under ``root``."""
    violations: list[Violation] = []
    owner_path = root / OWNER_PATH
    owner_tree = _parse(owner_path)
    owner_defs = [
        node
        for node in owner_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == OWNER_SYMBOL
    ]
    if len(owner_defs) != 1:
        violations.append(
            Violation(
                OWNER_PATH,
                1,
                f"{OWNER_MODULE}.{OWNER_SYMBOL} must have exactly one definition",
            )
        )

    for relative_path, required_functions in CONSUMER_FUNCTIONS.items():
        tree = _parse(root / relative_path)
        if not _imports_owner(tree):
            violations.append(
                Violation(
                    relative_path,
                    1,
                    f"must import {OWNER_SYMBOL} directly from {OWNER_MODULE}",
                )
            )

        functions = _qualnamed_functions(tree)
        for qualname, minimum_calls in required_functions.items():
            function = functions.get(qualname)
            if function is None:
                violations.append(
                    Violation(relative_path, 1, f"required consumer function missing: {qualname}")
                )
                continue
            call_count = _owner_call_count(function)
            if call_count < minimum_calls:
                violations.append(
                    Violation(
                        relative_path,
                        function.lineno,
                        f"{qualname} must delegate diagnostic names to "
                        f"{OWNER_MODULE}.{OWNER_SYMBOL} at least {minimum_calls} time(s)",
                    )
                )

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in {
                OWNER_SYMBOL,
                RETIRED_SYMBOL,
            }:
                violations.append(
                    Violation(
                        relative_path,
                        node.lineno,
                        f"must not define local diagnostic ASCII normalizer {node.name}",
                    )
                )
            elif isinstance(node, ast.Name) and node.id == RETIRED_SYMBOL:
                violations.append(
                    Violation(
                        relative_path,
                        node.lineno,
                        f"retired {RETIRED_SYMBOL} must not be restored",
                    )
                )
            elif _local_ascii_signal(node):
                violations.append(
                    Violation(
                        relative_path,
                        getattr(node, "lineno", 1),
                        f"must delegate printable-ASCII normalization to "
                        f"{OWNER_MODULE}.{OWNER_SYMBOL}, not reimplement it locally",
                    )
                )

    return sorted(violations, key=lambda item: (str(item.path), item.line, item.message))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check canonical ownership of printable agent diagnostic names."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the checker and return its process exit code."""
    args = _parse_args(argv)
    try:
        violations = check(args.root.resolve())
    except (FileNotFoundError, SyntaxError) as exc:
        print(f"[x] diagnostic ASCII owner check failed closed: {exc}")
        return 1
    if violations:
        for violation in violations:
            print(f"[x] {violation.render()}")
        return 1
    print("[+] diagnostic ASCII owner check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
