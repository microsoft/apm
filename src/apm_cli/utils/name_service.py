"""Read the name service switch configuration (``/etc/nsswitch.conf``).

An I/O boundary only: it reports which sources each database names. Callers
decide what those sources prove.
"""

from __future__ import annotations

from pathlib import Path

NSSWITCH = Path("/etc/nsswitch.conf")


def database_sources() -> dict[str, list[str]] | None:
    """Each database's listed sources and actions, or ``None`` when unreadable.

    A missing file (macOS reads its directory service instead) is ``None``.
    """
    try:
        text = NSSWITCH.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    sources: dict[str, list[str]] = {}
    for line in text.splitlines():
        database, sep, rest = line.split("#", 1)[0].partition(":")
        if sep:
            sources[database.strip()] = rest.split()
    return sources
