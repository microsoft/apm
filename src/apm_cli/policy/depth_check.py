"""The ``dependencies.max_depth`` verdict for chains an install stopped resolving (#4826)."""

from __future__ import annotations

from collections.abc import Sequence

from .models import CheckResult, PolicySubject


def depth_limit_check(chains: Sequence[str], max_depth: int) -> CheckResult:
    """The ``transitive-depth`` verdict for chains resolution cut at *max_depth* (#4826)."""
    from .policy_checks import _subjects_result

    return _subjects_result(
        "transitive-depth",
        f"{len(chains)} dependency chain(s) exceed max depth {max_depth}",
        [PolicySubject(chain, f"exceeds limit {max_depth}", remedy=None) for chain in chains],
    )
