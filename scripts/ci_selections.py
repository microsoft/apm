"""Canonical CI test selections (architecture owner ``ci-validation-topology``).

A selection named here is the one definition every workflow lane that runs it
routes through, so two lanes cannot drift apart. ``windows-unit`` is the
Windows unit suite: the required PR gate (``ci.yml`` Windows Compatibility
Gate shards) and the main-push and full-validation Windows jobs
(``build-release.yml``) run exactly this, so a pull request sees every Windows
unit node that main runs.

Usage::

    python -m scripts.ci_selections windows-unit [pytest args...]

Extra arguments (the ``--shard-count``/``--shard-index`` selector of
``scripts/ci_sharding.py``, ``--collect-only``) pass through to pytest.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

SELECTIONS: dict[str, tuple[str, ...]] = {
    "windows-unit": (
        "tests/unit",
        "tests/test_console.py",
        "-n",
        "4",
        "--dist",
        "worksteal",
        "-p",
        "scripts.ci_sharding",
        "--shard-durations",
        ".github/ci-durations/windows-unit.json",
        # Named starts/outcomes identify an unfinished test without dumping captures.
        "-vv",
        "--tb=short",
        "--show-capture=no",
        "--no-showlocals",
    ),
}


def pytest_args(name: str, extra: Sequence[str] = ()) -> list[str]:
    """Return the pytest arguments of one named selection plus caller extras."""
    if name not in SELECTIONS:
        raise KeyError(f"unknown CI selection {name!r}; known: {sorted(SELECTIONS)}")
    return [*SELECTIONS[name], *extra]


def main(argv: Sequence[str]) -> int:
    if not argv or argv[0] not in SELECTIONS:
        print(
            f"usage: python -m scripts.ci_selections {{{','.join(sorted(SELECTIONS))}}} "
            "[pytest args...]",
            file=sys.stderr,
        )
        return 2
    import pytest

    return int(pytest.main(pytest_args(argv[0], argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
