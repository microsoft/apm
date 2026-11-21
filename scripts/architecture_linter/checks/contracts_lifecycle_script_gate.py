"""Lifecycle scripts fire only through the script gate (APM_NO_SCRIPTS, org
``executables.deny_all``, project script trust).

``core/lifecycle_scripts.py`` (``build_runner_from_context``) is the only place
that constructs a ``LifecycleScriptRunner``. A runner built anywhere else fires
scripts the gate would have withheld, as ``apm lifecycle test --execute`` did.

The gate's org-policy read runs inside a ``try`` whose every handler suppresses
scripts: an error while resolving org policy never lets scripts fire with the
org ceiling unread.
"""

import ast

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    checked_facts,
    inventory_paths,
    source_text,
    violation,
)
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-lifecycle-script-gate"
POLICY_ERRORS_RULE_ID = "contracts-tooling-lifecycle-policy-errors"
_OWNER = "src/apm_cli/core/lifecycle_scripts.py"
_RUNNER = "LifecycleScriptRunner"


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    """Refuse every LifecycleScriptRunner construction outside the gate owner."""
    findings: list[Violation] = []
    owner_facts, failures = checked_facts(provider, _OWNER, RULE_ID, require_python=True)
    findings.extend(failures)
    if owner_facts is not None and not any(
        d.name == "build_runner_from_context" for d in owner_facts.definitions
    ):
        findings.append(violation(RULE_ID, _OWNER, "build_runner_from_context must exist"))
    for path in inventory_paths(provider, prefixes=("src/apm_cli/",)):
        if not path.endswith(".py"):
            continue
        facts, path_failures = checked_facts(provider, path, RULE_ID, require_python=True)
        findings.extend(path_failures)
        if facts is None:
            continue
        findings.extend(
            violation(
                RULE_ID,
                path,
                f"construct {_RUNNER} only through build_runner_from_context, which applies "
                "APM_NO_SCRIPTS, org deny_all and project script trust",
                line=call.line,
                column=call.column,
            )
            for call in facts.calls
            if call.qualname.rsplit(".", 1)[-1] == _RUNNER
            and (path != _OWNER or call.scope != "build_runner_from_context")
        )
    return tuple(findings)


_GATE = "build_runner_from_context"
_POLICY_CALLS = frozenset({"discover_policy_with_chain", "route_discovery_outcome"})
_SUPPRESSION = "suppressed_reason"


def _called_names(nodes: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for stmt in nodes:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    names.add(func.attr)
                elif isinstance(func, ast.Name):
                    names.add(func.id)
    return names


def _suppresses(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == _SUPPRESSION for t in node.targets)
        and not (isinstance(node.value, ast.Constant) and node.value.value is None)
        for node in ast.walk(handler)
    )


def _check_policy_errors(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every handler around the gate's org-policy read suppresses scripts."""
    facts, failures = checked_facts(provider, _OWNER, POLICY_ERRORS_RULE_ID, require_python=True)
    if failures:
        return failures
    tree = ast.parse(source_text(facts))
    gate = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == _GATE
        ),
        None,
    )
    if gate is None:
        return (violation(POLICY_ERRORS_RULE_ID, _OWNER, f"{_GATE} must exist"),)
    findings: list[Violation] = []
    guarded = False
    for node in ast.walk(gate):
        if not isinstance(node, ast.Try) or not _called_names(node.body) & _POLICY_CALLS:
            continue
        guarded = True
        findings.extend(
            violation(
                POLICY_ERRORS_RULE_ID,
                _OWNER,
                f"an error while reading org policy must set {_SUPPRESSION} so lifecycle "
                "scripts do not fire with the org ceiling unread",
                line=handler.lineno,
            )
            for handler in node.handlers
            if not _suppresses(handler)
        )
    if not guarded:
        findings.append(
            violation(
                POLICY_ERRORS_RULE_ID,
                _OWNER,
                f"{_GATE} must read org policy inside a try whose handlers set {_SUPPRESSION}",
                line=gate.lineno,
            )
        )
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        (RULE_ID,),
        "Lifecycle scripts fire only through the script gate owner.",
        _check,
    ),
    Rule(
        POLICY_ERRORS_RULE_ID,
        "contracts_tests",
        (POLICY_ERRORS_RULE_ID,),
        "An org-policy read error in the lifecycle script gate suppresses scripts.",
        _check_policy_errors,
    ),
)
