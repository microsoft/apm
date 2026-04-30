"""Semver parsing and range matching for marketplace builds.

Provides a minimal, dependency-free semver implementation that covers the
range formats used by ``marketplace.yml`` version constraints:

* Exact: ``"1.2.3"``
* Caret: ``"^1.2.3"`` (compatible with major)
* Tilde: ``"~1.2.3"`` (compatible with minor)
* Comparison: ``">=1.2.3"``, ``">1.2.3"``, ``"<=1.2.3"``, ``"<1.2.3"``
* Wildcard: ``"1.2.x"`` / ``"1.2.*"``
* Combined (AND): ``">=1.0.0 <2.0.0"``

Prerelease identifiers are compared per the semver 2.0.0 spec:
numeric identifiers sort before alphanumeric, and a prerelease version
always has lower precedence than the same version without a prerelease.
Build metadata is stored but ignored during comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional  # noqa: F401

__all__ = [
    "SemVer",
    "parse_semver",
    "satisfies_range",
]

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=False)
class SemVer:
    """Parsed semantic version.

    Instances are frozen, hashable, and support all comparison operators.
    Ordering follows the semver 2.0.0 specification.
    """

    major: int
    minor: int
    patch: int
    prerelease: str  # empty string means no prerelease
    build_meta: str  # ignored in comparisons

    @property
    def is_prerelease(self) -> bool:
        """Return ``True`` when this version carries a prerelease tag."""
        return self.prerelease != ""

    def _cmp_tuple(self) -> tuple:
        """Return a tuple suitable for comparison.

        Prerelease versions have lower precedence than their release
        counterpart.  When both have prerelease identifiers, they are
        compared lexicographically by dot-separated identifier.
        """
        if not self.prerelease:
            # Release: sorts after any prerelease of same major.minor.patch
            return (self.major, self.minor, self.patch, 1, ())
        parts: list[tuple[int, int, str]] = []
        for ident in self.prerelease.split("."):
            if ident.isdigit():
                parts.append((0, int(ident), ""))
            else:
                parts.append((1, 0, ident))
        return (self.major, self.minor, self.patch, 0, tuple(parts))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_tuple() < other._cmp_tuple()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_tuple() <= other._cmp_tuple()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_tuple() > other._cmp_tuple()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_tuple() >= other._cmp_tuple()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_tuple() == other._cmp_tuple()

    def __hash__(self) -> int:
        return hash(self._cmp_tuple())


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_semver(text: str) -> SemVer | None:
    """Parse a semver string into a ``SemVer`` instance.

    Returns ``None`` when *text* does not match the semver grammar.

    Examples
    --------
    >>> parse_semver("1.2.3")
    SemVer(major=1, minor=2, patch=3, prerelease='', build_meta='')
    >>> parse_semver("not-a-version") is None
    True
    """
    m = _SEMVER_RE.match(text)
    if not m:
        return None
    return SemVer(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        patch=int(m.group(3)),
        prerelease=m.group(4) or "",
        build_meta=m.group(5) or "",
    )


# ---------------------------------------------------------------------------
# Range matching
# ---------------------------------------------------------------------------


def satisfies_range(version: SemVer, range_spec: str) -> bool:
    """Check if *version* satisfies a semver range specification.

    Supported range formats (may be combined with spaces for AND):

    * Exact: ``"1.2.3"``
    * Caret: ``"^1.2.3"`` (``>=1.2.3``, ``<2.0.0``)
    * Tilde: ``"~1.2.3"`` (``>=1.2.3``, ``<1.3.0``)
    * Wildcard: ``"1.2.x"`` / ``"1.2.*"`` (``>=1.2.0``, ``<1.3.0``)
    * Comparison: ``">=1.2.3"``, ``">1.2.3"``, ``"<=1.2.3"``, ``"<1.2.3"``
    * Combined: ``">=1.0.0 <2.0.0"`` (space-separated AND)

    An empty *range_spec* matches everything.
    """
    spec = range_spec.strip()
    if not spec:
        return True

    # Space-separated constraints are AND-ed
    parts = spec.split()
    if len(parts) > 1:
        return all(_satisfies_single(version, p) for p in parts)
    return _satisfies_single(version, spec)


def _satisfies_single(version: SemVer, spec: str) -> bool:
    """Check a single constraint."""
    spec = spec.strip()
    if not spec:
        return True

    # Caret range: ^major.minor.patch
    if spec.startswith("^"):
        base = parse_semver(spec[1:])
        if base is None:
            return False
        if base.major != 0:
            # ^1.2.3 := >=1.2.3 <2.0.0
            return version >= base and version.major == base.major
        if base.minor != 0:
            # ^0.2.3 := >=0.2.3 <0.3.0
            return version >= base and version.major == 0 and version.minor == base.minor
        # ^0.0.3 := >=0.0.3 <0.0.4
        return (
            version >= base
            and version.major == 0
            and version.minor == 0
            and version.patch == base.patch
        )

    # Tilde range: ~major.minor.patch
    if spec.startswith("~"):
        base = parse_semver(spec[1:])
        if base is None:
            return False
        # ~1.2.3 := >=1.2.3 <1.3.0
        return version >= base and version.major == base.major and version.minor == base.minor

    # Comparison operators
    if spec.startswith(">="):
        base = parse_semver(spec[2:])
        return base is not None and version >= base
    if spec.startswith(">") and not spec.startswith(">="):
        base = parse_semver(spec[1:])
        return base is not None and version > base
    if spec.startswith("<="):
        base = parse_semver(spec[2:])
        return base is not None and version <= base
    if spec.startswith("<") and not spec.startswith("<="):
        base = parse_semver(spec[1:])
        return base is not None and version < base

    # Wildcard: 1.2.x or 1.2.*
    wildcard_match = re.match(r"^(\d+)\.(\d+)\.[xX*]$", spec)
    if wildcard_match:
        major = int(wildcard_match.group(1))
        minor = int(wildcard_match.group(2))
        return version.major == major and version.minor == minor

    # Exact match
    base = parse_semver(spec)
    if base is None:
        return False
    return (
        version.major == base.major
        and version.minor == base.minor
        and version.patch == base.patch
        and version.prerelease == base.prerelease
    )
