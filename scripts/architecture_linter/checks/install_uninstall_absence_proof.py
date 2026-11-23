"""Uninstall proves an aborted sync's removed file absent only through its owner (#4851).

After an aborted hook sync, ``_retain_failed_claims`` keeps every removed
file's lock claim unless ``hook_absence.absence_proven`` proves the file gone
under the deploy root ``uninstall`` pinned before changing anything. A
direct filesystem probe there would follow a swapped link and drop the claim.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Violation

GUARD_UNINSTALL_ABSENCE_PROOF = "install-deployment-uninstall-absence-proof"
_CLI = "src/apm_cli/commands/uninstall/cli.py"
_OWNER = "src/apm_cli/commands/uninstall/hook_absence.py"
# Every call _retain_failed_claims may make. absence_proven is its only
# filesystem-touching call; any other probe (os.access, islink, a directory
# scan, open, resolve) or local helper fails closed.
_ALLOWED_CALLS = frozenset(
    {
        "absence_proven",
        "Path",
        "list",
        "failed.extend",
        "removed_rows.values",
        # Lockfile reconciliation of the already-decided failed paths.
        "retain_failed_claims",
        "logger.warning",
        "logger.verbose_detail",
        "LOCKFILE_STATE_UNRECONCILED.sentence",
    }
)
_OWNER_LITERALS = (
    "os.O_NOFOLLOW",
    "dir_fd=handle",
    "!= pinned",
    "follow_symlinks=False",
)


# The one import each allowlisted name may be bound by: (level, module).
_BINDINGS = {"absence_proven": (1, "hook_absence"), "Path": (0, "pathlib")}


def _bindings(tree: ast.AST) -> dict[str, list[object]]:
    """Every binding of a _BINDINGS name: ``(level, module)`` for an import, else its node."""
    found: dict[str, list[object]] = {name: [] for name in _BINDINGS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in found:
                    origin = (
                        (node.level, node.module)
                        if isinstance(node, ast.ImportFrom) and alias.name == bound
                        else node
                    )
                    found[bound].append(origin)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name in found:
                found[node.name].append(node)
        elif isinstance(node, ast.arg) and node.arg in found:
            found[node.arg].append(node)
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and node.id in found
        ):
            found[node.id].append(node)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                if name in found:
                    found[name].append(node)
    return found


def _binding_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Bind each allowlisted name only through its owner import (keyed by binding)."""
    index = provider.tree_index(_CLI)
    tree = index.root if index is not None else None
    if tree is None:
        return [violation(rule_id, _CLI, "uninstall/cli.py must parse")]
    findings: list[Violation] = []
    for name, bindings in _bindings(tree).items():
        if bindings != [_BINDINGS[name]]:
            level, module = _BINDINGS[name]
            source = f"{'.' * level}{module}"
            findings.append(
                violation(
                    rule_id,
                    _CLI,
                    f"{name} must be bound only by 'from {source} import {name}'",
                )
            )
    return findings


def _calls_in(facts, name: str) -> list[str] | None:
    definitions = [item for item in facts.definitions if item.name == name]
    if len(definitions) != 1:
        return None
    start, end = definitions[0].line, definitions[0].end_line
    return [call.qualname for call in facts.calls if start <= call.line <= end]


def check_uninstall_absence_proof(provider: FactsProvider) -> tuple[Violation, ...]:
    """Route aborted-sync absence through the pinned, no-follow owner."""
    rule_id = GUARD_UNINSTALL_ABSENCE_PROOF
    findings: list[Violation] = []
    facts, failures = _facts_for(provider, _CLI, rule_id)
    findings.extend(failures)
    if not failures:
        retain = _calls_in(facts, "_retain_failed_claims")
        if retain is None or "absence_proven" not in retain:
            findings.append(
                violation(
                    rule_id, _CLI, "_retain_failed_claims must prove absence via absence_proven"
                )
            )
        else:
            findings.extend(
                violation(
                    rule_id,
                    _CLI,
                    f"_retain_failed_claims may decide absence only through absence_proven: {name}",
                )
                for name in sorted(set(retain) - _ALLOWED_CALLS)
            )
        findings.extend(_binding_violations(provider, rule_id))
        command = _calls_in(facts, "uninstall")
        if command is None or "root_identity" not in command:
            findings.append(
                violation(
                    rule_id, _CLI, "uninstall must pin root_identity before changing anything"
                )
            )
    owner, failures = _facts_for(provider, _OWNER, rule_id)
    findings.extend(failures)
    if not failures:
        text = source_text(owner)
        findings.extend(
            violation(rule_id, _OWNER, f"the absence proof must keep {literal}")
            for literal in _OWNER_LITERALS
            if literal not in text
        )
    return tuple(findings)
