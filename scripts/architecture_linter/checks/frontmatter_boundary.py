"""Frontmatter fence detection stays in utils/yaml_io.py (#5248).

A second fence detector outside the frontmatter owner would decide where a
block ends independently of the parser that reads it, so it is reported
under the frontmatter guard.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.contracts_test_shared import _python_paths
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Violation

_FENCE_NAMES = frozenset({"FM_BOUNDARY", "_BOUNDED_FRONTMATTER_HANDLER"})
_FENCE_MODULE = "frontmatter.default_handlers"


def _fence_use(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return node.module == _FENCE_MODULE
    if isinstance(node, ast.Attribute):
        return node.attr in _FENCE_NAMES
    return isinstance(node, ast.Name) and node.id in _FENCE_NAMES


def frontmatter_fence_uses(provider: FactsProvider, rule_id: str, owner: str) -> list[Violation]:
    """Report frontmatter fence detection anywhere under src/ but *owner*."""
    findings: list[Violation] = []
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == owner:
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures or facts.tree_index is None:
            continue
        findings.extend(
            violation(
                rule_id,
                path,
                "frontmatter fences are detected only in utils/yaml_io.py "
                "(use read_frontmatter_block or loads_frontmatter)",
                line=node.lineno,
                column=node.col_offset + 1,
            )
            for node in facts.tree_index.nodes
            if _fence_use(node)
        )
    return findings
