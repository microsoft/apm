"""The one audit message for a changed package copy (#4480).

Every owner that finds a package copy that no longer matches its locked
content (the ``apm audit --ci`` materialization check, native Agent Plugin
admission and the Codex native registrar) names the drift with this message,
so the same drift reads the same whichever owner detects it.
"""

from __future__ import annotations

PACKAGE_CACHE_DRIFT = "package cache differs from its locked content"


def package_cache_drift(subject: str, remedy: str) -> str:
    """Return the drift message for *subject*, followed by its *remedy*."""
    return f"{subject}: {PACKAGE_CACHE_DRIFT}; {remedy}"
