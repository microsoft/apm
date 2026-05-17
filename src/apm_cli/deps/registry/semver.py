"""Registry-facing wrappers around APM's canonical semver matcher.

Registry dependencies are not shipped yet, so they intentionally reuse the
existing marketplace semver grammar instead of carrying a separate dialect.
The public helpers here keep the registry resolver API small while delegating
all version parsing, ordering, and range matching to ``apm_cli.marketplace``.
"""

from __future__ import annotations

import re
from typing import List, Optional

from apm_cli.marketplace.semver import SemVer, parse_semver, satisfies_range

_RANGE_OPERATORS = (">=", "<=", ">", "<", "^", "~")
_WILDCARD_RE = re.compile(r"^\d+\.\d+\.[xX*]$")


def _is_range_component(component: str) -> bool:
    """Return whether one space-separated range component is syntactically valid."""
    if not component:
        return False
    if _WILDCARD_RE.match(component):
        return True
    for op in _RANGE_OPERATORS:
        if component.startswith(op):
            return parse_semver(component[len(op):]) is not None
    return parse_semver(component) is not None


def is_semver_range(spec: str) -> bool:
    """Return ``True`` iff *spec* is a valid semver version or range.

    Used at parse time to reject branch names, commit SHAs, and arbitrary refs
    when an entry routes through a registry.
    """
    parts = spec.strip().split()
    return bool(parts) and all(_is_range_component(part) for part in parts)


def match_version(spec: str, version: str) -> bool:
    """Return ``True`` iff *version* satisfies the semver range *spec*."""
    if not is_semver_range(spec):
        return False
    v = parse_semver(version)
    if v is None:
        return False
    return satisfies_range(v, spec)


def pick_best(spec: str, versions: List[str]) -> Optional[str]:
    """Return the highest *version* in *versions* that satisfies *spec*.

    Returns ``None`` if no version matches or the spec is invalid.
    """
    if not is_semver_range(spec):
        return None
    candidates: List[tuple[SemVer, str]] = []
    for raw in versions:
        v = parse_semver(raw)
        if v is None:
            continue
        if satisfies_range(v, spec):
            candidates.append((v, raw))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]
