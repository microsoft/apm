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
AGENT_CONSUMER = Path("src/apm_cli/integration/agent_integrator.py")
AGENT_DIAGNOSTIC_FUNCTIONS = {
    "AgentIntegrator._warn_codex_unverified_scope",
    "AgentIntegrator._warn_codex_tools_dropped",
}
OPENCODE_CONSUMER = Path("src/apm_cli/integration/opencode_frontmatter.py")
OPENCODE_FUNCTION = "validate_opencode_frontmatter"
CONSUMERS = (AGENT_CONSUMER, OPENCODE_CONSUMER)
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


def _is_owner_call(node: ast.AST, argument: ast.AST | None = None) -> bool:
    """Return whether ``node`` directly calls the owner on ``argument``."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == OWNER_SYMBOL
        and len(node.args) == 1
    ):
        return False
    return argument is None or ast.dump(node.args[0]) == ast.dump(argument)


def _source_name() -> ast.Attribute:
    """Return the expected ``source.name`` identity expression."""
    return ast.Attribute(value=ast.Name(id="source", ctx=ast.Load()), attr="name", ctx=ast.Load())


def _package_name() -> ast.Name:
    """Return the expected ``package_name`` identity expression."""
    return ast.Name(id="package_name", ctx=ast.Load())


def _contains_name(node: ast.AST, name: str) -> bool:
    """Return whether an expression contains a load of ``name``."""
    return any(
        isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id == name
        for child in ast.walk(node)
    )


def _diagnostic_calls(function: ast.AST) -> list[ast.Call]:
    """Return DiagnosticCollector calls that render one Codex warning."""
    return [
        node
        for node in _walk_own_scope(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"warn", "lossy_agent_compilation"}
    ]


def _identity_is_directly_owned_in_diagnostic(function: ast.AST) -> bool:
    """Return whether one diagnostic call owns source and package rendering."""
    for call in _diagnostic_calls(function):
        owner_calls = [node for node in ast.walk(call) if _is_owner_call(node)]
        if any(_is_owner_call(node, _source_name()) for node in owner_calls) and any(
            _is_owner_call(node, _package_name()) for node in owner_calls
        ):
            return True
    return False


def _assignments_to(function: ast.AST, name: str) -> list[ast.Assign]:
    """Return simple assignments to ``name`` in one function scope."""
    return [
        node
        for node in _walk_own_scope(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]


def _opencode_identity_flow_is_owned(function: ast.AST) -> bool:
    """Return whether OpenCode messages derive identity only from the owner."""
    safe_name_assignments = _assignments_to(function, "safe_name")
    identifier_assignments = _assignments_to(function, "identifier")
    if len(safe_name_assignments) != 1 or len(identifier_assignments) != 1:
        return False
    if not _is_owner_call(safe_name_assignments[0].value, _source_name()):
        return False
    identifier = identifier_assignments[0].value
    if not any(_is_owner_call(node, _package_name()) for node in ast.walk(identifier)):
        return False
    if not _contains_name(identifier, "safe_name"):
        return False
    message_appends = [
        node
        for node in _walk_own_scope(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "messages"
        and node.func.attr == "append"
    ]
    return bool(message_appends) and all(
        _contains_name(call, "identifier") for call in message_appends
    )


def _is_identity(node: ast.AST) -> bool:
    """Return whether ``node`` is source.name or package_name."""
    return ast.dump(node) in {ast.dump(_source_name()), ast.dump(_package_name())}


def _directly_consumes_identity(call: ast.Call) -> bool:
    """Return whether a call directly receives a diagnostic identity value."""
    if any(_is_identity(argument) for argument in call.args):
        return True
    if any(_is_identity(keyword.value) for keyword in call.keywords):
        return True
    return isinstance(call.func, ast.Attribute) and _is_identity(call.func.value)


def _raw_identity_lines(node: ast.AST) -> list[int]:
    """Return raw identity loads not owned by ``printable_ascii_text``.

    ``IfExp.test`` is control flow rather than rendered data, so a predicate
    such as ``... if package_name else ...`` is allowed. Both branches remain
    checked because either may contribute bytes to output.
    """
    lines: list[int] = []

    def visit(current: ast.AST, parent: ast.AST | None = None) -> None:
        if _is_identity(current):
            if not (isinstance(parent, ast.Call) and _is_owner_call(parent)):
                lines.append(getattr(current, "lineno", 1))
            return
        if isinstance(current, ast.IfExp):
            visit(current.body, current)
            visit(current.orelse, current)
            return
        for child in ast.iter_child_nodes(current):
            visit(child, current)

    visit(node)
    return lines


def _non_owner_identity_calls(function: ast.AST) -> list[ast.Call]:
    """Return calls that locally transform diagnostic identity inputs."""
    return [
        node
        for node in _walk_own_scope(function)
        if isinstance(node, ast.Call)
        and _directly_consumes_identity(node)
        and not _is_owner_call(node)
    ]


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

    for relative_path in CONSUMERS:
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
        expected_functions = (
            AGENT_DIAGNOSTIC_FUNCTIONS if relative_path == AGENT_CONSUMER else {OPENCODE_FUNCTION}
        )
        for qualname in expected_functions:
            function = functions.get(qualname)
            if function is None:
                violations.append(
                    Violation(relative_path, 1, f"required consumer function missing: {qualname}")
                )
                continue
            flow_is_owned = (
                _identity_is_directly_owned_in_diagnostic(function)
                if relative_path == AGENT_CONSUMER
                else _opencode_identity_flow_is_owned(function)
            )
            if not flow_is_owned:
                violations.append(
                    Violation(
                        relative_path,
                        function.lineno,
                        f"{qualname} must derive rendered diagnostic identity directly from "
                        f"{OWNER_MODULE}.{OWNER_SYMBOL}",
                    )
                )
            output_calls = (
                _diagnostic_calls(function)
                if relative_path == AGENT_CONSUMER
                else [
                    node
                    for node in _walk_own_scope(function)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "messages"
                    and node.func.attr == "append"
                ]
            )
            for output_call in output_calls:
                for line in _raw_identity_lines(output_call):
                    violations.append(
                        Violation(
                            relative_path,
                            line,
                            f"{qualname} must not render raw source.name or package_name",
                        )
                    )
            for assignment in (
                node
                for node in _walk_own_scope(function)
                if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr)
            ):
                value = assignment.value
                if value is None:
                    continue
                for line in _raw_identity_lines(value):
                    violations.append(
                        Violation(
                            relative_path,
                            line,
                            f"{qualname} must not assign raw diagnostic identity for later use",
                        )
                    )
            for call in _non_owner_identity_calls(function):
                violations.append(
                    Violation(
                        relative_path,
                        call.lineno,
                        f"{qualname} must not pass source.name or package_name through "
                        "a local normalization path",
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
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id == OWNER_SYMBOL
            ):
                violations.append(
                    Violation(
                        relative_path,
                        node.lineno,
                        f"must not shadow canonical owner {OWNER_SYMBOL}",
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
