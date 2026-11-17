"""Guard the dependency selection-merge owner (#3735).

Every edge the resolver discovers must be recorded for the selection merge
before any dedup can drop it, and the merge itself has exactly one owner.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _duplicate_definition_lines,
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_SELECTION_MERGE = "install-deployment-selection-merge"
_OWNER = "src/apm_cli/models/dependency/subsets.py"
_RESOLVER = "src/apm_cli/deps/apm_resolver.py"
_TREE_BUILDER = "APMDependencyResolver.build_dependency_tree"
_RECORD = "_record_edge_selection"


def _is_self_call(node: ast.AST, attribute: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    )


def _binds_edge(statement: ast.stmt) -> str | None:
    """Return the bound name when *statement* is ``X = self.replacement_index.bind(...)``."""
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and _is_self_call(statement.value, "bind")
    ):
        return statement.targets[0].id
    return None


def _records(statement: ast.stmt, name: str) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and _is_self_call(statement.value, _RECORD)
        and bool(statement.value.args)
        and isinstance(statement.value.args[0], ast.Name)
        and statement.value.args[0].id == name
    )


def _unrecorded_edges(function: ast.AST) -> tuple[int, ...]:
    """Return lines of edge-discovery sites not recorded by the next statement."""
    missing: list[int] = []
    for node in ast.walk(function):
        for field in ("body", "orelse"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for position, statement in enumerate(block):
                name = _binds_edge(statement)
                if name is None:
                    continue
                following = block[position + 1] if position + 1 < len(block) else None
                if following is None or not _records(following, name):
                    missing.append(statement.lineno)
    return tuple(missing)


def check_selection_merge(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require every resolver edge to reach the single selection-merge owner."""
    failures: list[Violation] = []
    required = {
        _RESOLVER: (
            "self._apply_edge_selections(tree)",
            "selection = merge_edge_selections(edges)",
        ),
        "src/apm_cli/deps/reachability.py": ("merge_edge_selections(edges)",),
        "src/apm_cli/policy/ci_checks.py": ("declared = declared_node_selections(",),
        "src/apm_cli/commands/uninstall/engine.py": ("declared = declared_node_selections(",),
        "src/apm_cli/install/package_resolution.py": (
            "return union_selection_names(persisted_subset, cli_subset)",
        ),
    }
    for path, fragments in required.items():
        facts, missing = _facts_for(provider, path, GUARD_SELECTION_MERGE)
        failures.extend(missing)
        if not missing and any(not _present(facts, fragment) for fragment in fragments):
            failures.append(
                _summary(
                    GUARD_SELECTION_MERGE,
                    path,
                    "Dependency selections must be merged by models/dependency/subsets.py",
                )
            )

    index = provider.tree_index(_RESOLVER)
    builder = index.function(_TREE_BUILDER) if index is not None else None
    if builder is None:
        failures.append(
            _summary(GUARD_SELECTION_MERGE, _RESOLVER, "build_dependency_tree is missing")
        )
    else:
        unrecorded = _unrecorded_edges(builder)
        sites = sum(
            1
            for node in ast.walk(builder)
            if isinstance(node, ast.stmt) and _binds_edge(node) is not None
        )
        if unrecorded or sites < 3:
            failures.append(
                _summary(
                    GUARD_SELECTION_MERGE,
                    _RESOLVER,
                    "Record every discovered edge with _record_edge_selection before dedup "
                    f"(unrecorded edge sites at lines {list(unrecorded)}, {sites} site(s) found)",
                )
            )

    failures.extend(
        _duplicate_definition_lines(
            provider,
            rule_id=GUARD_SELECTION_MERGE,
            prefix="src/apm_cli/",
            pattern=re.compile(r"^def (merge_edge_selections|union_selection_names)\("),
            owner=_OWNER,
            message="Dependency selection merging has one owner",
            respect_exempt=False,
        )
    )
    return tuple(failures)
