"""Source invariants for install exit-code ownership."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
INSTALL_ROOT = SRC_ROOT / "install"


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_exit_code(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "exit_code"


def _is_exit_sink(function: ast.AST) -> bool:
    if isinstance(function, ast.Name):
        return function.id == "SystemExit"
    if not isinstance(function, ast.Attribute):
        return False
    return function.attr in {"exit", "Exit", "SystemExit"}


def _qualified_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _click_exception_exit_calls(tree: ast.AST) -> set[tuple[int, int]]:
    """Return exit calls that map ClickException, not InstallResult."""
    calls = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if _qualified_name(node.type) != "click.ClickException":
            continue
        for statement in node.body:
            for child in ast.walk(statement):
                if isinstance(child, ast.Call) and _is_exit_sink(child.func):
                    calls.add((child.lineno, child.col_offset))
    return calls


def _result_exit_mappings(tree: ast.AST) -> list[ast.Call]:
    mappings = []
    click_exception_calls = _click_exception_exit_calls(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_exit_sink(node.func):
            continue
        if (node.lineno, node.col_offset) in click_exception_calls:
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(_is_exit_code(argument) for argument in arguments):
            mappings.append(node)
    return mappings


def test_only_install_command_maps_result_exit_code() -> None:
    """Exactly one command boundary translates InstallResult.exit_code."""
    owners: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        relative_path = path.relative_to(SRC_ROOT).as_posix()
        mappings = _result_exit_mappings(_tree(path))
        if mappings:
            owners.append(relative_path)

    assert owners == ["commands/install.py"]


def test_install_engine_never_maps_result_exit_code() -> None:
    """Install engine modules return InstallResult; only the command maps it."""
    offenders = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in INSTALL_ROOT.rglob("*.py")
        if _result_exit_mappings(_tree(path))
    ]

    assert not offenders


def test_negative_control_exit_mappings_are_detected() -> None:
    """Variable renames and each supported exit form must not bypass the guard."""
    snippets = (
        "ctx.exit(result.exit_code)",
        "sys.exit(outcome.exit_code)",
        "raise SystemExit(install_result.exit_code)",
        "raise click.exceptions.Exit(value.exit_code)",
    )

    for snippet in snippets:
        assert _result_exit_mappings(ast.parse(snippet)), snippet
