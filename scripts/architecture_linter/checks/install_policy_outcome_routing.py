"""Policy-outcome routing is closed-world (#4084).

* ``policy/outcome_routing.py`` owns the closed set of policy-discovery
  outcomes (``POLICY_DISCOVERY_OUTCOMES``) and gives every one exactly one
  route (``_OUTCOME_ROUTES``); an outcome outside the set fails closed.
* Discovery never emits an outcome outside that set.
* No module outside the owner classifies outcomes: it imports none of the
  owner's outcome sets and holds no outcome-membership list. The only
  registered exception renders ``apm policy status``.
* audit, approve, update and MCP admission ask the owner for the decision.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_POLICY_OUTCOME_ROUTING = "install-deployment-policy-outcome-routing"

_OWNER = "src/apm_cli/policy/outcome_routing.py"
_OUTCOME_SET = "POLICY_DISCOVERY_OUTCOMES"
_ROUTES = "_OUTCOME_ROUTES"
_OWNER_SETS = frozenset(
    {_OUTCOME_SET, _ROUTES, "POLICY_RESOLUTION_FAILURE_OUTCOMES", "POLICY_HASH_MISMATCH_OUTCOME"}
)
_OWNER_NEEDLES = (
    "def classify_discovery_outcome(",
    "route = classify_discovery_outcome(fetch_result, fetch_failure_default=fetch_failure_default)",
    'reason = f"unrecognized policy discovery outcome',
)
_CONSUMERS: dict[str, str] = {
    "src/apm_cli/commands/audit.py": "route = classify_discovery_outcome(",
    "src/apm_cli/commands/approve.py": "policy = resolve_trust_policy(project_root, logger=logger)",
    "src/apm_cli/commands/update.py": "route = classify_project_policy(",
    "src/apm_cli/security/trust.py": "route = classify_project_policy(policy_root)",
}
# Display-only readers that name outcomes to word a status line.
_DISPLAY_SCOPES = frozenset({("src/apm_cli/commands/policy.py", "_build_report")})


def _strings(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _owner_tables(index) -> tuple[set[str] | None, set[str] | None]:
    outcomes = routes = None
    for node in index.module_children():
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        name = getattr(target, "id", None)
        if name == _OUTCOME_SET:
            outcomes = set(_strings(value))
        elif name == _ROUTES and isinstance(value, ast.Dict):
            routes = {k.value for k in value.keys if isinstance(k, ast.Constant)}
    return outcomes, routes


def _emitted_outcomes(index) -> list[tuple[str, int]]:
    """Outcome string constants assigned to ``.outcome`` or passed as ``outcome=``."""
    found: list[tuple[str, int]] = []
    for node in index.nodes:
        if isinstance(node, ast.Call):
            callee = node.func
            name = getattr(callee, "id", None) or getattr(callee, "attr", None)
            if name == "PolicyFetchResult":
                for keyword in node.keywords:
                    if keyword.arg == "outcome":
                        found.extend((value, node.lineno) for value in _strings(keyword.value))
        elif isinstance(node, ast.Assign):
            if any(getattr(t, "attr", None) == "outcome" for t in node.targets):
                found.extend((value, node.lineno) for value in _strings(node.value))
    return found


def _membership_lists(index, outcomes: set[str]) -> list[tuple[int, str]]:
    """Collections naming two or more outcomes, or an ``in`` test against one."""
    hits: list[tuple[int, str]] = []
    displays = (ast.Tuple, ast.List, ast.Set)
    for node in index.nodes:
        if isinstance(node, displays):
            named = {value for value in _strings(node) if value in outcomes}
            if len(named) >= 2:
                hits.append((node.lineno, ", ".join(sorted(named))))
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
        ):
            for comparator in node.comparators:
                if isinstance(comparator, displays):
                    named = {value for value in _strings(comparator) if value in outcomes}
                    if len(named) == 1:
                        hits.append((node.lineno, next(iter(named))))
    return hits


def _scope_of(index, line: int) -> str:
    """Name of the innermost function enclosing *line* (``<module>`` if none)."""
    scope, start = "<module>", -1
    for node in index.nodes:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= line <= (node.end_lineno or node.lineno)
            and node.lineno > start
        ):
            scope, start = node.name, node.lineno
    return scope


def check_policy_outcome_routing(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every policy-discovery outcome routes through one closed-world owner."""
    rule = GUARD_POLICY_OUTCOME_ROUTING
    findings: list[Violation] = []
    owner, failures = _facts_for(provider, _OWNER, rule)
    if failures:
        return tuple(failures)
    for needle in _OWNER_NEEDLES:
        if not _present(owner, needle):
            findings.append(_summary(rule, _OWNER, f"The outcome routing owner lost: {needle}"))
    outcomes, routes = _owner_tables(owner.tree_index)
    if not outcomes:
        findings.append(_summary(rule, _OWNER, f"The owner must define {_OUTCOME_SET}"))
        return tuple(findings)
    if routes != outcomes:
        missing = sorted(outcomes - (routes or set()))
        extra = sorted((routes or set()) - outcomes)
        findings.append(
            _summary(
                rule,
                _OWNER,
                f"{_ROUTES} must route exactly {_OUTCOME_SET}; "
                f"unrouted: {missing or '-'}, unknown: {extra or '-'}",
            )
        )

    for path, needle in _CONSUMERS.items():
        facts, path_failures = _facts_for(provider, path, rule)
        findings.extend(path_failures)
        if not path_failures and not _present(facts, needle):
            findings.append(
                _summary(
                    rule, path, f"Policy readers ask the outcome routing owner; missing {needle}"
                )
            )

    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _OWNER:
            continue
        facts, path_failures = _facts_for(provider, path, rule)
        if path_failures or facts.tree_index is None:
            continue
        index = facts.tree_index
        findings.extend(
            violation(
                rule,
                path,
                f"Discovery emits outcome {value!r} outside {_OUTCOME_SET}",
                line=line,
            )
            for value, line in _emitted_outcomes(index)
            if value not in outcomes
        )
        findings.extend(
            violation(
                rule,
                path,
                f"Imports the owner's outcome table {name}; ask classify_discovery_outcome",
                line=item.line,
            )
            for item in facts.imports
            for name in item.names
            if name in _OWNER_SETS
        )
        for line, named in _membership_lists(index, outcomes):
            if (path, _scope_of(index, line)) in _DISPLAY_SCOPES:
                continue
            findings.append(
                violation(
                    rule,
                    path,
                    f"Classifies policy outcomes ({named}) outside policy/outcome_routing.py",
                    line=line,
                )
            )
    return tuple(findings)


GUARD_POLICY_FIELD_TYPES = "install-deployment-policy-field-types"

_PARSER = "src/apm_cli/policy/parser.py"
_ENUM_CHECKER = "_check_enums"
_PARSER_NEEDLES = (
    "        if not isinstance(value, str) or value not in valid:",
    "    _check_mappings(data, errors)",
    "    _check_enums(data, errors)",
    "    _check_scalars(data, errors)",
    "    _check_str_lists(data, errors)",
    "    except (TypeError, AttributeError, ValueError) as exc:",
)


def check_policy_field_types(provider: FactsProvider) -> tuple[Violation, ...]:
    """A wrongly typed policy field is a malformed policy, never a parser crash (#4140).

    Enum membership runs only in ``_check_enums`` after a string check, every
    ``_VALID_*`` set is a typed enum field, and ``load_policy`` turns any shape
    the checks missed into a validation error.
    """
    rule = GUARD_POLICY_FIELD_TYPES
    facts, failures = _facts_for(provider, _PARSER, rule)
    if failures:
        return tuple(failures)
    findings = [
        _summary(rule, _PARSER, f"The policy parser type checks lost: {needle.strip()}")
        for needle in _PARSER_NEEDLES
        if not _present(facts, needle)
    ]
    index = facts.tree_index
    if index is None:
        return tuple(findings)
    enum_sets = {
        node.targets[0].id
        for node in index.module_children()
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("_VALID_")
    }
    typed = {
        item.id
        for node in index.module_children()
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "_ENUM_FIELDS"
        for item in ast.walk(node)
        if isinstance(item, ast.Name)
    }
    findings.extend(
        _summary(rule, _PARSER, f"{name} must be checked as a typed enum field in _ENUM_FIELDS")
        for name in sorted(enum_sets - typed)
    )
    for node in index.nodes:
        if not isinstance(node, ast.Compare) or not any(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
        ):
            continue
        names = {getattr(c, "id", "") for c in node.comparators}
        if any(name.startswith("_VALID_") or name == "valid" for name in names) and (
            _scope_of(index, node.lineno) != _ENUM_CHECKER
        ):
            findings.append(
                violation(
                    rule,
                    _PARSER,
                    "Enum membership outside _check_enums can see an unhashable value",
                    line=node.lineno,
                )
            )
    return tuple(findings)
