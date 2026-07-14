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
PARITY_FACADE = "registry_docs_mismatches"
PARITY_INTERNALS = {
    "public_top_level_commands",
    "rendered_cli_reference_pages",
}
PARITY_OWNER_FUNCTIONS = {
    *PARITY_INTERNALS,
    PARITY_FACADE,
}


def _python_files(root: Path, locations: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for location in locations:
        base = root / location
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files.extend(base.rglob("*.py"))
    return sorted(path for path in files if path.is_file())


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


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_environment_get(call: ast.Call, variable: str) -> bool:
    called = _attribute_name(call.func)
    return (
        called in {"os.environ.get", "os.getenv"}
        and bool(call.args)
        and _literal_string(call.args[0]) == variable
    )


def _is_environment_subscript(node: ast.Subscript, variable: str) -> bool:
    return _attribute_name(node.value) == "os.environ" and _literal_string(node.slice) == variable


def _direct_binary_env_read_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and _is_environment_get(node, "APM_BINARY_PATH")) or (
            isinstance(node, ast.Subscript) and _is_environment_subscript(node, "APM_BINARY_PATH")
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _defined_functions(tree: ast.AST) -> set[str]:
    return {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _parse(path: Path, root: Path) -> tuple[ast.Module | None, str | None]:
    try:
        return ast.parse(path.read_text(encoding="utf-8")), None
    except (OSError, SyntaxError) as error:
        return None, f"[x] cannot inspect {path.relative_to(root)}: {error}"


def find_binary_selection_violations(root: Path) -> list[str]:
    """Reject every direct integration-test read outside the canonical owner."""
    diagnostics: list[str] = []
    owner = root / BINARY_OWNER
    owner_tree, owner_error = _parse(owner, root)
    if owner_error is not None:
        diagnostics.append(owner_error)
    elif owner_tree is None or "_resolve_apm_binary" not in _defined_functions(owner_tree):
        diagnostics.append(f"[x] {BINARY_OWNER} must define _resolve_apm_binary")

    integration_root = root / "tests" / "integration"
    for path in _python_files(root, ("tests/integration",)):
        if path == owner:
            continue
        tree, error = _parse(path, root)
        if error is not None:
            diagnostics.append(error)
            continue
        if tree is None:
            continue
        relative = path.relative_to(root).as_posix()
        for line in _direct_binary_env_read_lines(tree):
            diagnostics.append(
                f"[x] direct APM_BINARY_PATH read outside {BINARY_OWNER}: "
                f"{relative}:{line}; consume the apm_binary_path fixture"
            )
        if path.parent == integration_root and "_resolve_apm_binary" in _defined_functions(tree):
            diagnostics.append(
                f"[x] duplicate _resolve_apm_binary definition: {relative}; owner is {BINARY_OWNER}"
            )
    return sorted(diagnostics)


def _parity_import_violations(tree: ast.AST, relative: str) -> list[str]:
    diagnostics: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "scripts.check_cli_docs":
            for alias in node.names:
                if alias.name in PARITY_INTERNALS:
                    diagnostics.append(
                        f"[x] internal rendered parity projection imported: "
                        f"{relative}:{node.lineno} {alias.name}; "
                        f"consume {PARITY_FACADE}"
                    )
        elif isinstance(node, ast.ImportFrom) and node.module == "scripts":
            for alias in node.names:
                if alias.name == "check_cli_docs":
                    diagnostics.append(
                        f"[x] rendered parity module imported directly: "
                        f"{relative}:{node.lineno}; import {PARITY_FACADE} only"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "scripts.check_cli_docs":
                    diagnostics.append(
                        f"[x] rendered parity module imported directly: "
                        f"{relative}:{node.lineno}; import {PARITY_FACADE} only"
                    )
    return diagnostics


def _is_commands_items(call: ast.Call) -> bool:
    called = _attribute_name(call.func)
    return called is not None and called.endswith(".commands.items")


def _registry_projection_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_commands_items(node):
            lines.add(node.lineno)
    return sorted(lines)


def _path_string_segments(node: ast.AST) -> set[str]:
    return {value for child in ast.walk(node) if (value := _literal_string(child)) is not None}


def _rendered_cli_path_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not {"reference", "cli"}.issubset(_path_string_segments(value)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _rendered_inventory_lines(tree: ast.AST) -> list[int]:
    lines: set[int] = set()
    rendered_path_names = _rendered_cli_path_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            "glob",
            "iterdir",
            "rglob",
        }:
            continue
        parent = node.func.value
        if {"reference", "cli"}.issubset(_path_string_segments(parent)) or (
            isinstance(parent, ast.Name) and parent.id in rendered_path_names
        ):
            lines.add(node.lineno)
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if "index.html" not in _path_string_segments(node):
            continue
        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in {"is_file", "exists"}
            for child in ast.walk(node)
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _owner_definition_violations(root: Path) -> list[str]:
    owner = root / PARITY_OWNER
    tree, error = _parse(owner, root)
    if error is not None:
        return [error]
    if tree is None:
        return [f"[x] cannot inspect {PARITY_OWNER}"]
    missing = sorted(PARITY_OWNER_FUNCTIONS - _defined_functions(tree))
    return [
        f"[x] {PARITY_OWNER} must define rendered parity owner function: {name}" for name in missing
    ]


def find_rendered_parity_violations(root: Path) -> list[str]:
    """Enforce facade-only consumers and unique registry/page projections."""
    diagnostics = _owner_definition_violations(root)
    owner = root / PARITY_OWNER
    for path in _python_files(root, ("src/apm_cli", "scripts", "tests")):
        if path == owner:
            continue
        tree, error = _parse(path, root)
        if error is not None:
            diagnostics.append(error)
            continue
        if tree is None:
            continue
        relative = path.relative_to(root).as_posix()
        diagnostics.extend(_parity_import_violations(tree, relative))
        for line in _registry_projection_lines(tree):
            diagnostics.append(
                f"[x] direct Click command registry projection: {relative}:{line}; "
                f"consume {PARITY_FACADE}"
            )
        for line in _rendered_inventory_lines(tree):
            diagnostics.append(
                f"[x] direct rendered CLI route inventory: {relative}:{line}; "
                f"consume {PARITY_FACADE}"
            )
    return sorted(set(diagnostics))


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
