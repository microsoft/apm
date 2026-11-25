"""Routing edge of ``transport-platform-url-path-security`` for the no-op verdict.

The no-op verdict judges linked governed paths through the same owner as the
engine's fast path (diff_apply._within), utils/path_security.py, so the two
cannot diverge (#5438). Registered by ``transport_auth_platform`` under the same
rule id.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.transport_platform_shared import (
    _count_checks,
    _require_subs,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

_VERDICT_INPUTS = "src/apm_cli/install/verdict_inputs.py"


def verdict_link_routing(
    provider: FactsProvider, inv: frozenset[str], rule_id: str
) -> tuple[Violation, ...]:
    """verdict_inputs decides links through path_security and never re-implements it."""
    return (
        *_require_subs(
            provider,
            inv,
            rule_id,
            _VERDICT_INPUTS,
            ("has_symlink_component(root, root / parent)", "if is_link_stat(info):"),
            "The no-op verdict must judge links through utils/path_security.py",
        ),
        *_count_checks(
            provider,
            inv,
            rule_id,
            _VERDICT_INPUTS,
            (("sub", "S_ISLNK(", 0, "eq"), ("sub", "st_reparse_tag", 0, "eq")),
            "The no-op verdict must not re-implement the link predicate",
        ),
    )
