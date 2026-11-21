"""Explicit ``--policy`` source ownership guard (#1925).

``policy/explicit_source.py`` owns the per-invocation explicit policy source
and its tighten-only composition with discovered policy. Discovery composes
through it exactly once; only the ``apm install`` / ``apm update`` ``--policy``
option (commands/_helpers.py) binds the source; nothing else reads it.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_policy_shared import (
    _POLICY_DISCOVERY,
    _banned,
    _configured,
    _count_re,
    _report,
    _tree_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

RULE_EXPLICIT_POLICY_SOURCE = "install-deployment-explicit-policy-source-owner"

_OWNER = "src/apm_cli/policy/explicit_source.py"
_SCOPE_OPENERS = ("src/apm_cli/commands/_helpers.py",)
_SOURCE_TREE = "src/apm_cli/"
_COMPOSE_CALL = re.compile(r"\bcompose_explicit_policy\(")
_ACTIVE_READ = re.compile(r"\bactive_explicit_policy_source\(")
_SCOPE_OPEN = re.compile(r"\bexplicit_policy_scope\(")
_MERGE_CALL = re.compile(r"\binheritance\.merge_policies\(discovered\.policy, explicit\.policy\)")


def check_explicit_policy_source_owner(provider: FactsProvider) -> tuple[Violation, ...]:
    """Explicit policy sources compose only through policy/explicit_source.py."""
    rule_id = RULE_EXPLICIT_POLICY_SOURCE
    findings: list[Violation] = []
    owner, owner_fail = _configured(provider, _OWNER, rule_id)
    discovery, discovery_fail = _configured(provider, _POLICY_DISCOVERY, rule_id)
    findings.extend((*owner_fail, *discovery_fail))
    if owner_fail or discovery_fail:
        return tuple(findings)
    if _count_re(owner, _MERGE_CALL) != 1:
        findings.append(
            _report(
                rule_id,
                _OWNER,
                "compose_explicit_policy must merge the explicit source as the "
                "tighten-only child of the discovered policy",
            )
        )
    if _count_re(discovery, _COMPOSE_CALL) != 1:
        findings.append(
            _report(
                rule_id,
                _POLICY_DISCOVERY,
                "discover_policy_with_chain must compose the explicit source exactly once",
            )
        )
    others = _tree_python_paths(provider, _SOURCE_TREE, excluded=(_OWNER,))
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=others,
            pattern=_ACTIVE_READ,
            message="Read the explicit policy source only inside policy/explicit_source.py",
            configured=False,
            respect_exempt=False,
        )
    )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=tuple(path for path in others if path not in _SCOPE_OPENERS),
            pattern=_SCOPE_OPEN,
            message="Only the shared --policy option (commands/_helpers.py) binds a policy source",
            configured=False,
            respect_exempt=False,
        )
    )
    return tuple(findings)
