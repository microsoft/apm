"""Guard the dependency selection-merge owner (#3735, #3763).

Every edge the resolver discovers must be recorded for the selection merge
before any dedup can drop it, and the merge itself has exactly one owner.
The exact per-edge skills x targets pairing (OpenAPM req-rs-018) flows from
that owner through the lockfile, deploy, audit, uninstall and frozen replay;
no deploy call may drop it and fall back to the cross-product.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _duplicate_definition_lines,
    _facts_for,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_SELECTION_MERGE = "install-deployment-selection-merge"
_OWNER = "src/apm_cli/models/dependency/subsets.py"
_RESOLVER = "src/apm_cli/deps/apm_resolver.py"
_TREE_BUILDER = "APMDependencyResolver.build_dependency_tree"
_RECORD = "_record_edge_selection"
# Deploy entry points that must receive the pairing from the effective-selection owner.
_PAIRED_CALLS = ("integrate_package_primitives", "integrate_package_skill")
_PAIRING_KEYWORD = "skill_pairing"


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
        _OWNER: ("pairing=merge_edge_pairings(",),
        _RESOLVER: (
            "self._apply_edge_selections(tree)",
            "selection = merge_edge_selections(edges)",
            "subset_pairings=selection.subset_pairings,",
        ),
        "src/apm_cli/deps/reachability.py": ("merge_edge_selections(edges)",),
        "src/apm_cli/deps/lockfile.py": (
            "SkillTargetSelection.from_lock_fields(",
            "subset_pairings=self._exact_pairing_or_none(),",
        ),
        "src/apm_cli/policy/ci_checks.py": (
            "declared = declared_node_selections(",
            "locked_pairing = locked_dep.selection()",
        ),
        "src/apm_cli/commands/uninstall/engine.py": (
            "declared = declared_node_selections(",
            "unchanged = locked.selection() == selection.lock_selection()",
        ),
        "src/apm_cli/install/package_resolution.py": (
            "return union_selection_names(persisted_subset, cli_subset)",
            'pairing = getattr(dep_ref, "subset_pairings", None)',
        ),
        "src/apm_cli/install/service.py": ("recorded = locked.selection()",),
        "src/apm_cli/install/phases/integrate.py": ("InstallService.enforce_frozen_selections(",),
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

    failures.extend(_unpaired_deploy_calls(provider))
    failures.extend(
        _duplicate_definition_lines(
            provider,
            rule_id=GUARD_SELECTION_MERGE,
            prefix="src/apm_cli/",
            pattern=re.compile(
                r"^def (merge_edge_selections|merge_edge_pairings|union_selection_names)\("
            ),
            owner=_OWNER,
            message="Dependency selection merging has one owner",
            respect_exempt=False,
        )
    )
    return tuple(failures)


def _unpaired_deploy_calls(provider: FactsProvider) -> list[Violation]:
    """Report deploy calls that omit ``skill_pairing=`` (req-rs-018).

    Without the pairing every selected target would receive every selected
    skill, silently restoring the skills x targets cross-product.
    """
    failures: list[Violation] = []
    for path in _python_paths(provider, "src/apm_cli/"):
        facts = provider.file_facts(path)
        if not any(
            call.qualname.rsplit(".", 1)[-1] in _PAIRED_CALLS
            for call in getattr(facts, "calls", ())
        ):
            continue
        index = provider.tree_index(path)
        if index is None:
            continue
        for top in index.module_children():
            for node in index.walk(top):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name not in _PAIRED_CALLS:
                    continue
                if any(keyword.arg == _PAIRING_KEYWORD for keyword in node.keywords):
                    continue
                failures.append(
                    Violation(
                        rule_id=GUARD_SELECTION_MERGE,
                        path=path,
                        line=node.lineno,
                        column=node.col_offset + 1,
                        message=(
                            f"{name}() must pass skill_pairing= from "
                            "effective_dependency_selection (req-rs-018)"
                        ),
                    )
                )
    return failures
