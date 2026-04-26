"""Minimal npm-style semver matcher for registry version selection.

Supports the range shapes called out in docs/proposals/registry-api.md §3.3:

- Exact version:        ``1.0.0``, ``v1.0.0``
- Caret:                ``^1.2``, ``^1.0.0``
- Tilde:                ``~1.2.3``
- Comparison:           ``>=1``, ``<2``, ``>=1,<2``
- X-range / wildcard:   ``1.x``, ``1.2.x``, ``*``

The proposal explicitly rejects branch names (``main``), commit SHAs
(``abc123d``), and arbitrary strings (``latest``) at parse time when an entry
routes to a registry. ``is_semver_range()`` is the parse-time gate.

Why a vendored matcher instead of ``packaging.specifiers``: PEP 440 differs
from npm semver in surface ways (no ``^``/``~`` per se, x-range syntax, version
prefix). The proposal's example ranges are npm-style, and registries on the
wire are likely to mirror npm's grammar. Keeping the matcher small and
dependency-free avoids dragging in a heavyweight dep for a few operators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# A version is exactly: major[.minor[.patch]] with an optional leading ``v``.
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")

# A range component is one of:
#   - bare version (exact)
#   - operator + version: ^X[.Y[.Z]] | ~X[.Y[.Z]] | >=X | >X | <=X | <X | =X
#   - x-range: X.x, X.Y.x, X.*
#   - star: *
_RANGE_OPS = ("^", "~", ">=", "<=", ">", "<", "=")


@dataclass(frozen=True)
class _Bound:
    """A single comparison constraint, e.g. ``>=1.2.0``."""

    op: str  # one of: =, >=, >, <=, <
    major: int
    minor: int
    patch: int

    def matches(self, v: Tuple[int, int, int]) -> bool:
        target = (self.major, self.minor, self.patch)
        if self.op == "=":
            return v == target
        if self.op == ">":
            return v > target
        if self.op == ">=":
            return v >= target
        if self.op == "<":
            return v < target
        if self.op == "<=":
            return v <= target
        return False


def _parse_version(s: str) -> Optional[Tuple[int, int, int]]:
    """Parse ``v?X[.Y[.Z]]`` into ``(major, minor, patch)``. Missing parts default to 0."""
    m = _VERSION_RE.match(s.strip())
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) is not None else 0
    patch = int(m.group(3)) if m.group(3) is not None else 0
    return (major, minor, patch)


def _expand_component(comp: str) -> Optional[List[_Bound]]:
    """Expand one range component (e.g. ``^1.2``) into one or two bounds.

    Returns ``None`` if the component is not a valid semver range piece.
    """
    comp = comp.strip()
    if not comp:
        return None
    if comp == "*":
        return [_Bound(">=", 0, 0, 0)]

    # X-range: 1.x, 1.2.x, 1.*, 1.2.*
    x_match = re.match(r"^v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?$", comp)
    if x_match:
        major_s, minor_s, patch_s = x_match.group(1), x_match.group(2), x_match.group(3)
        if minor_s in ("x", "*", None) and patch_s in ("x", "*", None) and (
            minor_s in ("x", "*") or patch_s in ("x", "*") or minor_s is None
        ):
            # Skip if it's actually a fully concrete version like ``1.2.3``
            if minor_s not in (None, "x", "*") and patch_s not in (None, "x", "*"):
                pass  # fall through to exact below
            else:
                major = int(major_s)
                if minor_s in (None, "x", "*"):
                    return [
                        _Bound(">=", major, 0, 0),
                        _Bound("<", major + 1, 0, 0),
                    ]
                # minor concrete, patch wildcard
                minor = int(minor_s)
                return [
                    _Bound(">=", major, minor, 0),
                    _Bound("<", major, minor + 1, 0),
                ]

    # Operator-prefixed: ^1.2, ~1.2.3, >=1, <2, =1.0.0
    for op in _RANGE_OPS:
        if comp.startswith(op):
            rest = comp[len(op):]
            v = _parse_version(rest)
            if v is None:
                return None
            major, minor, patch = v
            if op == "^":
                # ^1.2.3 := >=1.2.3, <2.0.0
                # ^0.2.3 := >=0.2.3, <0.3.0  (npm semver rule for 0.x)
                # ^0.0.3 := >=0.0.3, <0.0.4
                if major > 0:
                    return [
                        _Bound(">=", major, minor, patch),
                        _Bound("<", major + 1, 0, 0),
                    ]
                if minor > 0:
                    return [
                        _Bound(">=", major, minor, patch),
                        _Bound("<", 0, minor + 1, 0),
                    ]
                return [
                    _Bound(">=", major, minor, patch),
                    _Bound("<", 0, 0, patch + 1),
                ]
            if op == "~":
                # ~1.2.3 := >=1.2.3, <1.3.0
                # ~1.2   := >=1.2.0, <1.3.0
                # ~1     := >=1.0.0, <2.0.0
                if "." in rest.lstrip("v"):
                    return [
                        _Bound(">=", major, minor, patch),
                        _Bound("<", major, minor + 1, 0),
                    ]
                return [
                    _Bound(">=", major, minor, patch),
                    _Bound("<", major + 1, 0, 0),
                ]
            return [_Bound(op, major, minor, patch)]

    # Bare version: exact
    v = _parse_version(comp)
    if v is None:
        return None
    return [_Bound("=", *v)]


def _parse_range(spec: str) -> Optional[List[_Bound]]:
    """Parse a comma-separated range expression into a flat list of bounds.

    Returns ``None`` if any component is invalid. A returned list is treated as
    a conjunction (all bounds must hold).
    """
    if not spec or not spec.strip():
        return None
    bounds: List[_Bound] = []
    for piece in spec.split(","):
        sub = _expand_component(piece)
        if sub is None:
            return None
        bounds.extend(sub)
    return bounds


def is_semver_range(spec: str) -> bool:
    """Return ``True`` iff *spec* is a valid semver version or range.

    Used at parse time to reject branch names, commit SHAs, and arbitrary refs
    when an entry routes through a registry (design §3.3).
    """
    return _parse_range(spec) is not None


def match_version(spec: str, version: str) -> bool:
    """Return ``True`` iff *version* satisfies the semver range *spec*."""
    bounds = _parse_range(spec)
    if bounds is None:
        return False
    v = _parse_version(version)
    if v is None:
        return False
    return all(b.matches(v) for b in bounds)


def pick_best(spec: str, versions: List[str]) -> Optional[str]:
    """Return the highest *version* in *versions* that satisfies *spec*.

    Returns ``None`` if no version matches or the spec is invalid.
    """
    bounds = _parse_range(spec)
    if bounds is None:
        return None
    candidates: List[Tuple[Tuple[int, int, int], str]] = []
    for raw in versions:
        v = _parse_version(raw)
        if v is None:
            continue
        if all(b.matches(v) for b in bounds):
            candidates.append((v, raw))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]
