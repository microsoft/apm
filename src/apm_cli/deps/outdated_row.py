"""Shared row type for ``apm outdated`` results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OutdatedRow:
    """One row of ``apm outdated`` output."""

    package: str
    current: str
    latest: str
    status: str
    extra_tags: list[str] = field(default_factory=list)
    source: str = ""
