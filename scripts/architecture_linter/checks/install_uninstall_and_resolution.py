"""Uninstall-selection and resolution-replacement install analyzers.

Ports two owner guards recorded in
``.apm/architecture/owners/install-deployment.json``:
``install-deployment-uninstall-selection`` and
``install-deployment-resolution-replacement``.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _UNINSTALL_ENGINE,
    _all_names,
    _count_re,
    _facts_for,
    _line_findings,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

_GUARD_RESOLUTION_REPLACEMENT = "install-deployment-resolution-replacement"


_GUARD_UNINSTALL_SELECTION = "install-deployment-uninstall-selection"


def _call_terminal_name(node: ast.Call) -> str | None:
    """Return the terminal callable name (Name.id or Attribute.attr)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_SELECTION_OWNER = "src/apm_cli/models/dependency/selection.py"


_OWNED_SELECTION_DEFS = frozenset({"parse_dependency_entry", "select_manifest_dependency"})


_FORBIDDEN_SELECTION_CALLS = frozenset(
    {"get_identity", "get_unique_key", "parse", "parse_from_dict"}
)


def _top_level_function(index: TreeIndex, name: str) -> ast.AST | None:
    """Return the single top-level function named `name`, or None."""
    if index.root is None:
        return None
    matches = [
        node
        for node in index.children(index.root)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def _validator_selection_findings(
    provider: FactsProvider, index: TreeIndex, rule_id: str
) -> list[Violation]:
    """Port _validator_violations for uninstall/engine.py::_validate_uninstall_packages."""
    validator = _top_level_function(index, "_validate_uninstall_packages")
    if validator is None:
        return [
            _summary(
                rule_id,
                _UNINSTALL_ENGINE,
                "expected exactly one _validate_uninstall_packages() definition",
            )
        ]
    findings: list[Violation] = []
    canonical_calls = [
        node
        for node in index.walk(validator)
        if isinstance(node, ast.Call) and _call_terminal_name(node) == "select_manifest_dependency"
    ]
    if len(canonical_calls) != 1:
        findings.append(
            violation(
                rule_id,
                _UNINSTALL_ENGINE,
                "_validate_uninstall_packages() must call select_manifest_dependency() exactly once",
                line=getattr(validator, "lineno", 1),
            )
        )
    dependency_names = {"current_deps"}
    changed = True
    while changed:
        changed = False
        for node in index.walk(validator):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            source_names = _all_names(index, node.value)
            if source_names.intersection(dependency_names) and target.id not in dependency_names:
                dependency_names.add(target.id)
                changed = True
    for loop in index.walk(validator):
        if isinstance(loop, (ast.For, ast.AsyncFor, ast.comprehension)) and _all_names(
            index, loop.iter
        ).intersection(dependency_names):
            findings.append(
                violation(
                    rule_id,
                    _UNINSTALL_ENGINE,
                    "manifest dependencies must be selected by dependency/selection.py, "
                    "not iterated in _validate_uninstall_packages()",
                    line=getattr(loop, "lineno", 1),
                )
            )
    for node in index.walk(validator):
        if isinstance(node, ast.Call) and _call_terminal_name(node) in _FORBIDDEN_SELECTION_CALLS:
            findings.append(
                violation(
                    rule_id,
                    _UNINSTALL_ENGINE,
                    "identity parsing/matching in _validate_uninstall_packages() must route "
                    "through dependency/selection.py",
                    line=getattr(node, "lineno", 1),
                )
            )
    return findings


def check_uninstall_selection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Uninstall selection must route through models/dependency/selection.py."""
    rule_id = _GUARD_UNINSTALL_SELECTION
    owner, owner_fail = _facts_for(provider, _SELECTION_OWNER, rule_id)
    engine, engine_fail = _facts_for(provider, _UNINSTALL_ENGINE, rule_id)
    if owner_fail or engine_fail:
        return tuple(list(owner_fail) + list(engine_fail))

    findings: list[Violation] = []
    # AC4 lexical guards.
    findings.extend(
        _line_findings(
            engine,
            _UNINSTALL_ENGINE,
            rule_id,
            re.compile(r"for dep_entry in current_deps|dep_ref\.get_identity\(\) == pkg_identity"),
            "Uninstall selection must route through dependency/selection.py",
            respect_exempt=False,
        )
    )
    if (
        _count_re(owner, re.compile(r"^def select_manifest_dependency\(")) != 1
        or _count_re(engine, re.compile(r"^[ \t]*selection = select_manifest_dependency\(")) != 1
        or not _present(owner, "dependency = parse_dependency_entry(entry)")
    ):
        findings.append(
            _summary(
                rule_id,
                _SELECTION_OWNER,
                "Uninstall selection must route through dependency/selection.py",
            )
        )
    # check_uninstall_selection_owner: owner definitions present.
    owner_index = provider.tree_index(_SELECTION_OWNER)
    owner_defs = (
        {
            node.name
            for node in owner_index.children(owner_index.root)
            if isinstance(node, FUNCTION_NODES)
        }
        if owner_index is not None and owner_index.root is not None
        else set()
    )
    missing = sorted(_OWNED_SELECTION_DEFS - owner_defs)
    if missing:
        findings.append(
            _summary(
                rule_id,
                _SELECTION_OWNER,
                f"missing owned definitions: {', '.join(missing)}",
            )
        )
    # Owned definitions must not appear outside the owner (any scope).
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _SELECTION_OWNER:
            continue
        facts = provider.file_facts(path)
        for definition in getattr(facts, "definitions", ()):
            if definition.name in _OWNED_SELECTION_DEFS and definition.kind in (
                "function",
                "async_function",
            ):
                findings.append(
                    violation(
                        rule_id,
                        path,
                        f"{definition.name}() belongs in {_SELECTION_OWNER}",
                        line=definition.line,
                    )
                )
    engine_index = provider.tree_index(_UNINSTALL_ENGINE)
    if engine_index is not None:
        findings.extend(_validator_selection_findings(provider, engine_index, rule_id))
    return tuple(findings)


_RESOLUTION_OWNER = "src/apm_cli/install/resolution_staging.py"


_RESOLUTION_CONSUMER = "src/apm_cli/install/phases/resolve.py"


_RESOLUTION_METHODS = frozenset(
    {"prepare_replacement", "publish_replacement", "discard_replacement"}
)


def check_resolution_replacement(provider: FactsProvider) -> tuple[Violation, ...]:
    """Resolution replacements must stay staged until the canonical publish boundary."""
    rule_id = _GUARD_RESOLUTION_REPLACEMENT
    owner, owner_fail = _facts_for(provider, _RESOLUTION_OWNER, rule_id)
    _consumer, consumer_fail = _facts_for(provider, _RESOLUTION_CONSUMER, rule_id)
    if owner_fail or consumer_fail:
        return tuple(list(owner_fail) + list(consumer_fail))

    findings: list[Violation] = []
    owner_defs = {
        definition.name
        for definition in getattr(owner, "definitions", ())
        if definition.kind in ("function", "async_function")
    }
    missing = sorted(_RESOLUTION_METHODS - owner_defs)
    if missing:
        findings.append(
            _summary(rule_id, _RESOLUTION_OWNER, f"owner is missing methods: {', '.join(missing)}")
        )
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _RESOLUTION_OWNER:
            continue
        facts = provider.file_facts(path)
        duplicates = sorted(
            {
                definition.name
                for definition in getattr(facts, "definitions", ())
                if definition.name in _RESOLUTION_METHODS
                and definition.kind in ("function", "async_function")
            }
        )
        if duplicates:
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"duplicates owner methods: {', '.join(duplicates)}",
                    line=1,
                )
            )

    index = provider.tree_index(_RESOLUTION_CONSUMER)
    if index is None or index.root is None:
        return tuple(findings)
    call_attribute_funcs = [
        node.func
        for node in index.walk(index.root)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    if not any(func.attr == "prepare_replacement" for func in call_attribute_funcs):
        findings.append(
            _summary(
                rule_id,
                _RESOLUTION_CONSUMER,
                "resolve phase does not prepare replacements through the owner",
            )
        )
    if any(
        func.attr == "prepare_path"
        and isinstance(func.value, ast.Name)
        and func.value.id == "staging_session"
        for func in call_attribute_funcs
    ):
        findings.append(
            _summary(
                rule_id,
                _RESOLUTION_CONSUMER,
                "resolve phase eagerly removes a live path before replacement",
            )
        )
    activation_routes = [
        keyword.value
        for node in index.walk(index.root)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "activation_callback"
    ]
    routes_owner_directly = any(
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "staging_session"
        and value.attr == "publish_replacement"
        for value in activation_routes
    )
    routes_owner_through_acceptance = any(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "partial"
        and bool(value.args)
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "_activate_validated_candidate"
        for value in activation_routes
    ) and any(
        func.attr == "publish_replacement"
        and isinstance(func.value, ast.Name)
        and func.value.id == "staging_session"
        for func in call_attribute_funcs
    )
    if not (routes_owner_directly or routes_owner_through_acceptance):
        findings.append(
            _summary(
                rule_id,
                _RESOLUTION_CONSUMER,
                "validated candidates do not publish through the staging owner",
            )
        )
    return tuple(findings)
