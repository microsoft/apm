"""Close-match suggestion helpers for user-facing CLI errors."""

from __future__ import annotations

import difflib
from collections.abc import Iterable

_CLOSE_MATCH_COUNT = 3
_CLOSE_MATCH_CUTOFF = 0.6


def close_name_matches(query: str, candidates: Iterable[str] | None) -> list[str]:
    """Return up to three close matches for *query* among *candidates*.

    Uses the same ``difflib.get_close_matches`` cutoff and count as
    experimental flag-name suggestions. Returns no matches when the
    candidate list cannot be read, rather than raising.
    """
    if not query:
        return []
    try:
        names = [str(name) for name in (candidates or []) if name]
    except Exception:
        return []
    if not names:
        return []

    by_lower: dict[str, str] = {}
    for name in names:
        by_lower.setdefault(name.lower(), name)

    try:
        matches = difflib.get_close_matches(
            query.lower(),
            list(by_lower.keys()),
            n=_CLOSE_MATCH_COUNT,
            cutoff=_CLOSE_MATCH_CUTOFF,
        )
    except Exception:
        return []
    return [by_lower[match] for match in matches]


def format_close_match_hint(
    suggestions: list[str],
    *,
    similar_label: str = "Similar",
) -> str:
    """Format close-match suggestions for appending to an error message."""
    if not suggestions:
        return ""
    if len(suggestions) == 1:
        return f" Did you mean: {suggestions[0]}?"
    return f" {similar_label}: {', '.join(suggestions)}"
