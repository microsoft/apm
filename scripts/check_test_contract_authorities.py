#!/usr/bin/env python3
"""Guard canonical owners for integration binaries and rendered CLI parity."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BINARY_OWNER = Path("tests/integration/conftest.py")
PARITY_OWNER = Path("scripts/check_cli_docs.py")


def _python_files(root: Path, locations: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for location in locations:
        base = root / location
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files.extend(base.rglob("*.py"))
    return sorted(path for path in files if path.is_file())


def _function_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _attribute_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _string_values(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _calls_named(node: ast.AST, names: set[str]) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        called = _attribute_name(child.func)
        if called is not None and called in names:
            return True
    return False


def _reads_binary_path_environment(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    if "APM_BINARY_PATH" not in _string_values(function):
        return False
    if _calls_named(function, {"os.environ.get", "os.getenv"}):
        return True
    return any(
        isinstance(node, ast.Subscript)
        and _attribute_name(node.value) == "os.environ"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "APM_BINARY_PATH"
        for node in ast.walk(function)
    )


def _binary_duplicate_reason(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    strings = _string_values(function)
    reads_binary_env = _reads_binary_path_environment(function)
    discovers_path_binary = _calls_named(function, {"shutil.which"}) and "apm" in strings
    discovers_venv_binary = ".venv" in strings and "apm" in strings

    if function.name == "_resolve_apm_binary":
        return "defines a second _resolve_apm_binary helper"
    if reads_binary_env and (discovers_path_binary or discovers_venv_binary):
        return "combines APM_BINARY_PATH reading with local/PATH fallback discovery"
    return None


def find_binary_selection_violations(root: Path) -> list[str]:
    """Find integration-test functions that reimplement binary selection."""
    diagnostics: list[str] = []
    owner = root / BINARY_OWNER
    if not owner.is_file() or "def _resolve_apm_binary(" not in owner.read_text(encoding="utf-8"):
        diagnostics.append(f"[x] {BINARY_OWNER} must define _resolve_apm_binary")

    for path in _python_files(root, ("tests",)):
        if path == owner:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as error:
            diagnostics.append(f"[x] cannot inspect {path.relative_to(root)}: {error}")
            continue
        for function in _function_nodes(tree):
            reason = _binary_duplicate_reason(function)
            if reason is not None:
                relative = path.relative_to(root).as_posix()
                diagnostics.append(
                    f"[x] duplicate integration binary selection: "
                    f"{relative}:{function.lineno} {function.name} {reason}"
                )
    return sorted(diagnostics)


def _has_visible_registry_projection(tree: ast.AST) -> bool:
    has_commands_items = False
    has_hidden_filter = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _attribute_name(node.func)
            if called is not None and called.endswith(".commands.items"):
                has_commands_items = True
        if isinstance(node, ast.Attribute) and node.attr == "hidden":
            has_hidden_filter = True
    return has_commands_items and has_hidden_filter


def _has_rendered_page_projection(tree: ast.AST) -> bool:
    strings = _string_values(tree)
    has_iterdir = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "iterdir"
        for node in ast.walk(tree)
    )
    return has_iterdir and {"reference", "cli", "index.html"}.issubset(strings)


def _has_bidirectional_set_difference(tree: ast.AST) -> bool:
    return (
        sum(isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) for node in ast.walk(tree))
        >= 2
    )


def find_rendered_parity_violations(root: Path) -> list[str]:
    """Find modules outside the checker that recompute rendered CLI parity."""
    diagnostics: list[str] = []
    owner = root / PARITY_OWNER
    owner_source = owner.read_text(encoding="utf-8") if owner.is_file() else ""
    required = (
        "def public_top_level_commands(",
        "def rendered_cli_reference_pages(",
        "def registry_docs_mismatches(",
    )
    if any(token not in owner_source for token in required):
        diagnostics.append(f"[x] {PARITY_OWNER} must own all rendered parity projections")

    for path in _python_files(root, ("src/apm_cli", "scripts")):
        if path == owner:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as error:
            diagnostics.append(f"[x] cannot inspect {path.relative_to(root)}: {error}")
            continue
        roles = (
            _has_visible_registry_projection(tree),
            _has_rendered_page_projection(tree),
            _has_bidirectional_set_difference(tree),
        )
        if sum(roles) >= 2:
            relative = path.relative_to(root).as_posix()
            diagnostics.append(
                f"[x] duplicate rendered CLI parity computation: {relative} "
                "must delegate to scripts/check_cli_docs.py"
            )
    return sorted(diagnostics)


def check(root: Path) -> list[str]:
    """Return all canonical-owner violations for the repository."""
    return [
        *find_binary_selection_violations(root),
        *find_rendered_parity_violations(root),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    diagnostics = check(args.root.resolve())
    for diagnostic in diagnostics:
        print(diagnostic)
    if diagnostics:
        print(f"[x] {len(diagnostics)} test contract authority violation(s) found")
        return 1
    print("[+] test contract authority check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
