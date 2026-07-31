#!/usr/bin/env python3
"""Enforce one owner for restriction-only package target authorization."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
from pathlib import Path

_OWNER = Path("src/apm_cli/install/target_filter.py")
_SERVICES_CONSUMER = Path("src/apm_cli/install/services.py")
_HOOK_CONSUMER = Path("src/apm_cli/integration/hook_integrator.py")
_UNINSTALL_CONSUMER = Path("src/apm_cli/commands/uninstall/engine.py")
_TARGET_ATTRIBUTES = frozenset({"target", "targets", "canonical_targets"})
_PACKAGE_INFO_NAMES = frozenset({"package_info", "pkg_info"})
_EXEMPTION = "architecture-authority-exempt:"


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    """Return a simple dotted attribute chain, or an empty tuple."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ()
    parts.append(current.id)
    return tuple(reversed(parts))


def _package_aliases(tree: ast.AST) -> set[str]:
    """Find local names assigned from package_info.package."""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            chain = _attribute_chain(value)
            from_package_info = (
                len(chain) == 2
                and chain == (chain[0], "package")
                and (chain[0] in _PACKAGE_INFO_NAMES)
            )
            from_alias = isinstance(value, ast.Name) and value.id in aliases
            if not from_package_info and not from_alias:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _is_exempt(lines: list[str], line_number: int) -> bool:
    """Return whether a finding line carries the explicit authority exemption."""
    return 0 < line_number <= len(lines) and _EXEMPTION in lines[line_number - 1]


def find_parallel_target_reads(paths: Iterable[Path]) -> list[str]:
    """Return semantic package-target reads outside the canonical owner."""
    violations: list[str] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:
            violations.append(f"{path}: unreadable Python: {exc}")
            continue
        lines = source.splitlines()
        aliases = _package_aliases(tree)
        for node in ast.walk(tree):
            if _is_exempt(lines, getattr(node, "lineno", 0)):
                continue
            if isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name) and function.id == "canonical_package_targets":
                    violations.append(
                        f"{path}:{node.lineno}: canonical_package_targets call outside owner"
                    )
            if not isinstance(node, ast.Attribute) or node.attr not in _TARGET_ATTRIBUTES:
                continue
            chain = _attribute_chain(node)
            direct = len(chain) == 3 and chain[0] in _PACKAGE_INFO_NAMES and chain[1] == "package"
            aliased = isinstance(node.value, ast.Name) and node.value.id in aliases
            if direct or aliased:
                violations.append(f"{path}:{node.lineno}: {ast.unparse(node)}")
    return sorted(set(violations))


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find one function or method by name."""
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ),
        None,
    )


def consumer_routes_through_selector(path: Path, function_name: str) -> bool:
    """Return whether a consumer uses the selector result as target input."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return False
    function = _find_function(tree, function_name)
    if function is None:
        return False
    assigned = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "target_selection"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "resolve_effective_package_targets"
        for node in ast.walk(function)
    )
    selector_iterables = {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            _attribute_chain(candidate) == ("target_selection", "targets")
            for candidate in ast.walk(node.value)
            if isinstance(candidate, ast.Attribute)
        )
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    drives_loop = any(
        (
            _attribute_chain(node.iter) == ("target_selection", "targets")
            or (isinstance(node.iter, ast.Name) and node.iter.id in selector_iterables)
        )
        for node in ast.walk(function)
        if isinstance(node, (ast.For, ast.AsyncFor))
    )
    return assigned and drives_loop


def _owner_is_complete(path: Path) -> bool:
    """Return whether the owner computes declared and effective targets."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return False
    function = _find_function(tree, "resolve_effective_package_targets")
    if function is None:
        return False
    declared_assignment = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "declared_targets"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "canonical_package_targets"
        for node in ast.walk(function)
    )
    effective_assignment = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "effective_targets"
            for target in node.targets
        )
        for node in ast.walk(function)
    )
    return declared_assignment and effective_assignment


def check(root: Path) -> list[str]:
    """Validate owner presence, consumer routing, and duplicate-read absence."""
    owner = root / _OWNER
    services = root / _SERVICES_CONSUMER
    hooks = root / _HOOK_CONSUMER
    uninstall = root / _UNINSTALL_CONSUMER
    required = (owner, services, hooks, uninstall)
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        return [f"missing required path: {path}" for path in missing]

    violations: list[str] = []
    if not _owner_is_complete(owner):
        violations.append(f"{_OWNER}: incomplete resolve_effective_package_targets owner")
    if not consumer_routes_through_selector(services, "integrate_package_primitives"):
        violations.append(f"{_SERVICES_CONSUMER}: selector result does not drive dispatch")
    if not consumer_routes_through_selector(hooks, "reconcile_after_removal"):
        violations.append(f"{_HOOK_CONSUMER}: selector result does not drive hook rebuild")
    if not consumer_routes_through_selector(uninstall, "_sync_integrations_after_uninstall"):
        violations.append(f"{_UNINSTALL_CONSUMER}: selector result does not drive survivor rebuild")

    candidates = sorted((root / "src/apm_cli/install").rglob("*.py"))
    candidates.extend(sorted((root / "src/apm_cli/integration").rglob("*.py")))
    candidates.append(uninstall)
    violations.extend(find_parallel_target_reads(path for path in candidates if path != owner))
    return violations


def main() -> int:
    """Run the package target authority check."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    violations = check(args.root.resolve())
    if violations:
        for violation in violations:
            print(violation)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
