"""Shared rule-factory helpers for contract/test analyzers.

``_owner_rule``/``_structural_rule`` and the generic fact-reading primitives
here are used by every cohesive contract/test check-family module
(:mod:`contracts_test_taxonomy`, :mod:`contracts_scope_binding`,
:mod:`contracts_structural_authorities`).
"""

from __future__ import annotations

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation


def _lines(facts: object) -> tuple[str, ...]:
    """Return the cached lexical lines for a file."""
    return getattr(facts, "lines", ())


def _present(facts: object, needle: str) -> bool:
    """Return whether any single line contains `needle` (literal, grep -q)."""
    return any(needle in line for line in _lines(facts))


def _python_paths(provider: FactsProvider, prefix: str) -> tuple[str, ...]:
    """Return every inventory Python path under `prefix`, in inventory order."""
    return tuple(
        path for path in provider.inventory if path.startswith(prefix) and path.endswith(".py")
    )


def _summary(rule_id: str, path: str, message: str) -> Violation:
    """Return a single owner-attributed summary violation."""
    return violation(rule_id, path, message, line=1)


_APM_EXECUTABLE_NAMES = frozenset({"apm", "apm.cmd", "apm.exe"})
