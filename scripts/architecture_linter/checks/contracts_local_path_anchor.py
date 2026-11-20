"""Guard the one install-time anchor decision for relative local paths (#4153).

``deps/path_anchoring.local_dep_base_dir`` decides what a dependency's
``local_path`` resolves against: the declaring package, the project source
root, or nothing (a root-declared relative path at user scope). The guard is
closed-world: no other function may combine a user-scope test with an
``is_absolute()`` test on a ``local_path``, nor restate the refusal text, and the three install
consumers must call the owner.
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


RULES = (
    Rule(
        id=RULE,
        group="contracts_tests",
        guard_ids=(RULE,),
        description="Relative local paths anchor on the declaring package via one owner.",
        check=check_local_path_anchor,
    ),
)
