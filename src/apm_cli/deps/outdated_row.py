"""Shared row type for ``apm outdated`` results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OutdatedRow:
    """One row of ``apm outdated`` output.

    Registry ``latest`` is the highest published semver. ``wanted`` retains
    the constraint-bound result, or "-" when unavailable; None omits the
    registry-specific column for other sources. ``outside_constraint`` refers
    to latest, not to the installed version.
    """

    package: str
    current: str
    latest: str
    status: str
    extra_tags: list[str] = field(default_factory=list)
    source: str = ""
    wanted: str | None = None
    outside_constraint: bool = False
