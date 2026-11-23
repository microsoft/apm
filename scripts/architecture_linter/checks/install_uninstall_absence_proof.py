"""Uninstall proves an aborted sync's removed file absent only through its owner (#4851).

After an aborted hook sync, ``_retain_failed_claims`` keeps every removed
file's lock claim unless ``hook_absence.absence_proven`` proves the file gone
under the deploy root ``uninstall`` pinned before changing anything. A
direct filesystem probe there would follow a swapped link and drop the claim.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.checks.install_uninstall_preflight import _FIRST_WRITES
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
# The pinned-root comparison; the no-follow flags are checked per call.
_OWNER_LITERALS = ("!= pinned",)


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


def _callee(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _function(tree: ast.AST, name: str) -> ast.AST | None:
    found = [
        node
        for node in getattr(tree, "body", [])
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    return found[0] if len(found) == 1 else None


def _named(nodes, kind, name: str) -> list[ast.AST]:
    return [node for node in nodes if isinstance(node, kind) and _callee(node) == name]


def _stores(function: ast.AST, name: str) -> list[ast.Name]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
    ]


def _pin_violations(tree: ast.AST, rule_id: str) -> list[Violation]:
    """The root is pinned once, before the first write, and reaches the proof unchanged."""
    command, retain = _function(tree, "uninstall"), _function(tree, "_retain_failed_claims")
    if command is None or retain is None:
        return [
            violation(
                rule_id, _CLI, "uninstall and _retain_failed_claims must each be defined once"
            )
        ]
    findings: list[Violation] = []
    nodes = list(ast.walk(command))
    pins = [
        node
        for node in nodes
        if isinstance(node, ast.Assign)
        and [getattr(target, "id", None) for target in node.targets] == ["root_pin"]
        and _callee(node.value) == "root_identity"
    ]
    if len(pins) != 1 or len(_named(nodes, ast.Call, "root_identity")) != 1:
        findings.append(
            violation(
                rule_id, _CLI, "uninstall must pin root_pin = root_identity(...) exactly once"
            )
        )
    elif len(_stores(command, "root_pin")) != 1:
        findings.append(violation(rule_id, _CLI, "uninstall must never rebind root_pin"))
    else:
        writes = [node.lineno for node in nodes if _callee(node) in _FIRST_WRITES]
        if not writes or pins[0].lineno > min(writes):
            findings.append(
                violation(rule_id, _CLI, "uninstall must pin root_pin before its first write")
            )
    handoffs = _named(nodes, ast.Call, "_retain_failed_claims")
    if len(handoffs) != 1 or not any(
        isinstance(arg, ast.Tuple)
        and any(isinstance(item, ast.Name) and item.id == "root_pin" for item in arg.elts)
        for arg in handoffs[0].args
    ):
        findings.append(
            violation(
                rule_id, _CLI, "uninstall must pass root_pin unchanged to _retain_failed_claims"
            )
        )
    proofs = _named(list(ast.walk(retain)), ast.Call, "absence_proven")
    if (
        len(_stores(retain, "root_pin")) != 1
        or not proofs
        or not all(
            any(
                keyword.arg == "pinned"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "root_pin"
                for keyword in proof.keywords
            )
            for proof in proofs
        )
    ):
        findings.append(
            violation(rule_id, _CLI, "absence_proven must receive pinned=root_pin unchanged")
        )
    return findings


def _has_no_follow_flag(call: ast.Call) -> bool:
    return len(call.args) > 1 and any(
        isinstance(node, ast.Attribute) and node.attr == "O_NOFOLLOW"
        for node in ast.walk(call.args[1])
    )


def _owner_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Every open in the proof is no-follow, and every lookup through a handle too."""
    index = provider.tree_index(_OWNER)
    tree = index.root if index is not None else None
    if tree is None:
        return [violation(rule_id, _OWNER, "hook_absence.py must parse")]
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    opens = [call for call in calls if _is_os(call, "open")]
    lookups = [
        call
        for call in calls
        if _is_os(call, "stat") and any(keyword.arg == "dir_fd" for keyword in call.keywords)
    ]
    findings = [
        violation(rule_id, _OWNER, "every os.open in the absence proof must carry O_NOFOLLOW")
        for call in opens
        if not _has_no_follow_flag(call)
    ]
    findings.extend(
        violation(rule_id, _OWNER, "every os.stat through a handle must pass follow_symlinks=False")
        for call in lookups
        if not any(
            keyword.arg == "follow_symlinks"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
    )
    walk = [call for call in opens if any(keyword.arg == "dir_fd" for keyword in call.keywords)]
    if len(opens) < 2 or not walk or not lookups:
        findings.append(
            violation(rule_id, _OWNER, "the absence proof must open the root and walk by handle")
        )
    return findings


def _is_os(call: ast.Call, name: str) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == name
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


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
        index = provider.tree_index(_CLI)
        if index is not None and index.root is not None:
            findings.extend(_pin_violations(index.root, rule_id))
    owner, failures = _facts_for(provider, _OWNER, rule_id)
    findings.extend(failures)
    if not failures:
        findings.extend(_owner_violations(provider, rule_id))
        text = source_text(owner)
        findings.extend(
            violation(rule_id, _OWNER, f"the absence proof must keep {literal}")
            for literal in _OWNER_LITERALS
            if literal not in text
        )
    return tuple(findings)
