#!/usr/bin/env python3
"""Enforce canonical deployment-owner reconciliation and cleanup boundaries."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

_REQUIRED_CALLS = {
    "prune.py": {"reconcile_owner_references"},
    "audit.py": {"owner_reference_violations"},
    "ci_checks.py": {"owner_reference_violations"},
}
_OWNER_ATTRIBUTES = {"owners", "active_owner", "deployment_ledger"}
_UNTRUSTED_NAME_PARTS = {
    "ghost",
    "invalid",
    "removed_record",
    "violation",
}


def _call_name(node: ast.Call) -> str:
    """Return the terminal callable name for one call expression."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _target_names(node: ast.AST) -> set[str]:
    """Collect names bound by an assignment or loop target."""
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}


def _loaded_names(node: ast.AST) -> set[str]:
    """Collect names loaded by an expression."""
    return {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }


def _contains_dependency_claim(node: ast.AST) -> bool:
    """Return whether an expression reads canonical dependency cleanup claims."""
    return any(
        isinstance(item, ast.Attribute) and item.attr in {"deployed_files", "deployed_file_hashes"}
        for item in ast.walk(node)
    )


def _trusted_claim_names(tree: ast.AST) -> set[str]:
    """Trace names derived from dependency deployed-file claims."""
    trusted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if _contains_dependency_claim(value) or (_loaded_names(value) & trusted):
                    before = len(trusted)
                    for target in targets:
                        trusted.update(_target_names(target))
                    changed = changed or len(trusted) != before
            elif isinstance(node, ast.For):
                if _loaded_names(node.iter) & trusted:
                    before = len(trusted)
                    trusted.update(_target_names(node.target))
                    changed = changed or len(trusted) != before
    return trusted


class _BoundaryVisitor(ast.NodeVisitor):
    """Find split owner authority and untrusted cleanup data flow."""

    def __init__(self, *, trusted_claims: set[str]) -> None:
        self.trusted_claims = trusted_claims
        self.calls: set[str] = set()
        self.violations: list[str] = []

    def _report(self, node: ast.AST, message: str) -> None:
        self.violations.append(f"line {getattr(node, 'lineno', 0)}: {message}")

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        self.calls.add(name)
        if name == "DeploymentRecord":
            self._report(
                node,
                "deployment consumers must not construct canonical records",
            )
        if name in {"set", "frozenset"} and any(
            isinstance(item, ast.Attribute) and item.attr == "dependencies"
            for argument in node.args
            for item in ast.walk(argument)
        ):
            self._report(
                node,
                "valid deployment owners must come from DeploymentLedgerCodec",
            )
        if name == "remove_stale_deployed_files" and node.args:
            cleanup_expression = node.args[0]
            names = _loaded_names(cleanup_expression)
            if not names.intersection(self.trusted_claims):
                self._report(
                    node,
                    "deployed-file cleanup must derive from dependency claims",
                )
            reads_ledger_locator = any(
                isinstance(item, ast.Attribute) and item.attr == "locator"
                for item in ast.walk(cleanup_expression)
            )
            if reads_ledger_locator or any(
                part in loaded.lower() for loaded in names for part in _UNTRUSTED_NAME_PARTS
            ):
                self._report(
                    node,
                    "ledger violations must not authorize physical deletion",
                )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_owner_mutation(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_owner_mutation(node.target)
        self.generic_visit(node)

    def _check_owner_mutation(self, target: ast.AST) -> None:
        for item in ast.walk(target):
            if isinstance(item, ast.Attribute) and item.attr in _OWNER_ATTRIBUTES:
                self._report(
                    item,
                    f"direct {item.attr} mutation bypasses DeploymentLedgerCodec",
                )

    def visit_comprehension(self, node: ast.comprehension) -> None:
        if self._iterates_owners(node.iter):
            self._report(
                node,
                "owner filtering must delegate to DeploymentReconciler",
            )
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if self._iterates_owners(node.iter):
            self._report(
                node,
                "owner filtering must delegate to DeploymentReconciler",
            )
        self.generic_visit(node)

    @staticmethod
    def _iterates_owners(node: ast.AST) -> bool:
        return any(
            isinstance(item, ast.Attribute) and item.attr == "owners" for item in ast.walk(node)
        )


def analyze_source(source: str, *, filename: str) -> list[str]:
    """Return deployment-owner boundary violations for Python source."""
    tree = ast.parse(source, filename=filename)
    visitor = _BoundaryVisitor(trusted_claims=_trusted_claim_names(tree))
    visitor.visit(tree)
    required = _REQUIRED_CALLS.get(Path(filename).name, set())
    for call in sorted(required - visitor.calls):
        visitor.violations.append(f"line 0: required canonical call {call} is missing")
    return visitor.violations


def analyze_path(path: Path) -> list[str]:
    """Return deployment-owner boundary violations for one file."""
    return analyze_source(
        path.read_text(encoding="utf-8"),
        filename=str(path),
    )


def main() -> int:
    """Run the checker over explicit consumer paths."""
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    violations = [f"{path}:{violation}" for path in args.paths for violation in analyze_path(path)]
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
