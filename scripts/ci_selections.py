"""Canonical CI test selections (architecture owner ``ci-validation-topology``).

A selection named here is the one definition every workflow lane that runs it
routes through, so two lanes cannot drift apart. ``windows-unit`` is the
Windows unit suite: the required PR gate (``ci.yml`` Windows Compatibility
Gate shards) and the main-push and full-validation Windows jobs
(``build-release.yml``) run exactly this, so a pull request sees every Windows
unit node that main runs.

Usage::

    python -m scripts.ci_selections windows-unit [pytest args...]

``--shard-count N --shard-index I`` runs shard I of N: the selection's roots
are replaced by that shard's explicit test files from
``scripts.ci_sharding.shard_test_files`` (balanced by the selection's
``--shard-durations`` seed), so a shard imports only its own modules. Other
extra arguments (``--collect-only``) pass through to pytest.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SHARD_FLAGS = ("--shard-count", "--shard-index")

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


def selection_roots(name: str) -> list[str]:
    """The test roots a selection collects (its leading positional arguments)."""
    roots: list[str] = []
    for arg in SELECTIONS[name]:
        if arg.startswith("-"):
            break
        roots.append(arg)
    return roots


def _option(args: Sequence[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def shard_pytest_args(name: str, extra: Sequence[str] = ()) -> list[str]:
    """pytest arguments for one selection, with a shard selector turned into its file list."""
    args = pytest_args(name, extra)
    count, index = (_option(extra, flag) for flag in _SHARD_FLAGS)
    if count is None or index is None:
        return args
    from scripts.ci_sharding import load_durations, shard_test_files

    roots = selection_roots(name)
    plan = shard_test_files(
        roots,
        _option(args, "-m"),
        int(count),
        load_durations(_option(args, "--shard-durations")),
        ROOT,
    )
    # The file list is the shard; the node-level sharding plugin is not loaded.
    dropped = {"-p": "scripts.ci_sharding", "--shard-durations": None}
    dropped.update(dict.fromkeys(_SHARD_FLAGS))
    rest: list[str] = []
    skip = False
    for position, arg in enumerate(args[len(roots) :], len(roots)):
        if skip:
            skip = False
        elif arg in dropped and dropped[arg] in (None, args[position + 1]):
            skip = True
        else:
            rest.append(arg)
    return [*plan.shards[int(index) - 1], *rest]


def main(argv: Sequence[str]) -> int:
    if not argv or argv[0] not in SELECTIONS:
        print(
            f"usage: python -m scripts.ci_selections {{{','.join(sorted(SELECTIONS))}}} "
            "[pytest args...]",
            file=sys.stderr,
        )
        return 2
    import pytest

    return int(pytest.main(shard_pytest_args(argv[0], argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
