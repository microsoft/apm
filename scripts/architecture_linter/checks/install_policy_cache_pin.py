"""Cached org policy is verified before use, in one reader (#4218).

* ``policy/discovery.py`` is the only module that locates the policy cache
  (``_get_cache_dir`` / ``POLICY_CACHE_DIR``).
* ``_read_cache_entry`` is the only function there that parses a cached
  policy document. It checks the document against the fingerprint its
  metadata recorded (``_cached_policy_matches``) before parsing, and parses
  the verified text, never the file a second time.
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

GUARD_POLICY_CACHE_PIN = "install-deployment-policy-cache-pin"

_OWNER = "src/apm_cli/policy/discovery.py"
_READER = "_read_cache_entry"
_CACHE_NAMES = frozenset({"_get_cache_dir", "POLICY_CACHE_DIR"})
_OWNER_NEEDLES = (
    "def _cached_policy_matches(serialized: str, meta: dict) -> bool:",
    "hmac.compare_digest(recorded, _policy_fingerprint(serialized))",
    "if not _cached_policy_verified(serialized, meta, expected_hash):",
    "return _cached_pin_matches(raw_bytes_hash, expected_hash) and _cached_policy_matches(",
    "policy, _warnings = load_policy(serialized)",
)


def _names(index, node: ast.AST) -> set[str]:
    found: set[str] = set()
    for item in index.walk(node):
        if isinstance(item, ast.Name):
            found.add(item.id)
        elif isinstance(item, ast.Attribute):
            found.add(item.attr)
    return found


def check_policy_cache_pin(provider: FactsProvider) -> tuple[Violation, ...]:
    """The policy cache has one reader, and it verifies a document before use."""
    rule = GUARD_POLICY_CACHE_PIN
    owner, failures = _facts_for(provider, _OWNER, rule)
    if failures:
        return tuple(failures)
    findings = [
        _summary(rule, _OWNER, f"The policy cache reader lost: {needle}")
        for needle in _OWNER_NEEDLES
        if not _present(owner, needle)
    ]
    index = owner.tree_index
    if index is not None:
        for function in index.functions():
            if function.name == _READER:
                continue
            names = _names(index, function)
            if "_get_cache_dir" in names and "load_policy" in names:
                findings.append(
                    violation(
                        rule,
                        _OWNER,
                        f"{function.name} parses a cached policy outside {_READER}",
                        line=function.lineno,
                    )
                )
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _OWNER:
            continue
        facts, failed = _facts_for(provider, path, rule)
        if failed:
            findings.extend(failed)
            continue
        other = facts.tree_index
        if other is None:
            continue
        for node in other.nodes:
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if isinstance(node, (ast.Name, ast.Attribute)) and name in _CACHE_NAMES:
                findings.append(
                    violation(
                        rule,
                        path,
                        f"{name} locates the policy cache outside {_OWNER}",
                        line=node.lineno,
                    )
                )
    return tuple(findings)
