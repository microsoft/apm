"""Guard the one install-time anchor decision for relative local paths (#4153).

``deps/path_anchoring.local_dep_base_dir`` decides what a dependency's
``local_path`` resolves against: the declaring package, the project source
root, or nothing (a root-declared relative path at user scope). The guard is
closed-world: no other function may combine a user-scope test with an
``is_absolute()`` test on a ``local_path``, nor restate the refusal text, and the three install
consumers must call the owner. ``deps/local_identity.transitive_local_identity``
likewise owns the node key of a declared local edge (#4731).
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.contracts_test_shared import _summary
from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import Rule, Violation

RULE = "contracts-tooling-local-path-anchor"
OWNER = "src/apm_cli/deps/path_anchoring.py"
OWNER_SYMBOL = "local_dep_base_dir"
_REFUSAL_TEXT = "relative local paths are not supported"
CONSUMERS = {
    "src/apm_cli/install/phases/local_content.py": "copy_local_dependency",
    "src/apm_cli/install/sources.py": "LocalDependencySource.acquire",
    "src/apm_cli/install/package_resolution.py": "user_scope_rejection_reason",
}


def _names_user_scope(node: ast.AST) -> bool:
    for item in ast.walk(node):
        if isinstance(item, ast.Attribute) and item.attr == "USER":
            return True
        if isinstance(item, ast.Name) and item.id == "user_scope":
            return True
    return False


def _mentions_local_path(node: ast.AST) -> bool:
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and "local_path" in item.id:
            return True
        if isinstance(item, ast.Attribute) and "local_path" in item.attr:
            return True
    return False


def _tests_absolute(node: ast.AST) -> bool:
    """Whether *node* asks if a dependency ``local_path`` is absolute."""
    return any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "is_absolute"
        and _mentions_local_path(item.func.value)
        for item in ast.walk(node)
    )


def _calls_owner(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == OWNER_SYMBOL
        for item in ast.walk(node)
    )


def _open_coded(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == OWNER:
            continue
        facts = provider.file_facts(path)
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        if _REFUSAL_TEXT in "\n".join(getattr(facts, "lines", ())):
            findings.append(_summary(RULE, path, f"Refusal text restated; import it from {OWNER}"))
        for node in ast.walk(index.root):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.Module(body=node.body, type_ignores=[])
            if _names_user_scope(body) and _tests_absolute(body) and not _calls_owner(body):
                findings.append(
                    _summary(
                        RULE,
                        path,
                        f"{node.name} decides a user-scope local path anchor; "
                        f"call {OWNER_SYMBOL} ({OWNER})",
                    )
                )
    return findings


def check_local_path_anchor(provider: FactsProvider) -> tuple[Violation, ...]:
    """Relative local paths anchor only through ``local_dep_base_dir``."""
    findings = _open_coded(provider)
    for path, symbol in CONSUMERS.items():
        facts, failures = checked_facts(provider, path, RULE, require_python=True)
        findings.extend(failures)
        if facts is None or facts.tree_index is None:
            continue
        function = facts.tree_index.function(symbol)
        if function is None or not _calls_owner(function):
            findings.append(_summary(RULE, path, f"{symbol} must anchor via {OWNER_SYMBOL}"))
    return tuple(findings)


IDENTITY_RULE = "contracts-tooling-local-identity"
IDENTITY_OWNER = "src/apm_cli/deps/local_identity.py"
IDENTITY_SYMBOL = "transitive_local_identity"
_ANCHOR_HELPERS = frozenset({"portable_anchor_identity", "_portable_anchor_identity"})
_ANCHOR_DELEGATE = ("src/apm_cli/deps/apm_resolver.py", "_portable_anchor_identity")
IDENTITY_CONSUMERS = {
    "src/apm_cli/deps/apm_resolver.py": "APMDependencyResolver.build_dependency_tree",
    "src/apm_cli/install/local_content_verdict.py": "local_content_reasons",
    "src/apm_cli/deps/reachability.py": "compute_forward_reachable_keys",
}


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls(node: ast.AST, names: frozenset[str]) -> bool:
    return any(
        isinstance(item, ast.Call) and _called_name(item) in names for item in ast.walk(node)
    )


def _anchor_bypasses(provider: FactsProvider) -> list[Violation]:
    """Only the identity owner may turn an anchored directory into a node key."""
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == IDENTITY_OWNER:
            continue
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        for node in index.functions():
            if (path, getattr(node, "name", "")) == _ANCHOR_DELEGATE:
                continue
            body = ast.Module(body=node.body, type_ignores=[])
            if _calls(body, _ANCHOR_HELPERS):
                findings.append(
                    _summary(
                        IDENTITY_RULE,
                        path,
                        f"{node.name} derives a local anchored identity; "
                        f"call {IDENTITY_SYMBOL} ({IDENTITY_OWNER})",
                    )
                )
    return findings


def check_local_identity(provider: FactsProvider) -> tuple[Violation, ...]:
    """One anchored directory is one dependency, keyed only by the identity owner."""
    findings = _anchor_bypasses(provider)
    for path, symbol in IDENTITY_CONSUMERS.items():
        facts, failures = checked_facts(provider, path, IDENTITY_RULE, require_python=True)
        findings.extend(failures)
        if facts is None or facts.tree_index is None:
            continue
        function = facts.tree_index.function(symbol)
        if function is None or not _calls(function, frozenset({IDENTITY_SYMBOL})):
            findings.append(
                _summary(
                    IDENTITY_RULE, path, f"{symbol} must key local edges via {IDENTITY_SYMBOL}"
                )
            )
    return tuple(findings)


RULES = (
    Rule(
        id=RULE,
        group="contracts_tests",
        guard_ids=(RULE,),
        description="Relative local paths anchor on the declaring package via one owner.",
        check=check_local_path_anchor,
    ),
    Rule(
        id=IDENTITY_RULE,
        group="contracts_tests",
        guard_ids=(IDENTITY_RULE,),
        description="One anchored local directory is one dependency, keyed by one owner.",
        check=check_local_identity,
    ),
)
