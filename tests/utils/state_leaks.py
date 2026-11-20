"""Per-test process-state leak detection.

The root ``tests/conftest.py`` snapshots this state around every non-integration
test and fails the test that leaves it changed, then restores it so the leak
cannot reach the next test on the same xdist worker. A leak here once made a
later test read empty stdout: a ``--format json`` CLI call left console output
routed to stderr for the rest of the worker.

Snapshots stay cheap: one ``os.environ`` dict copy, ``os.getcwd()`` and a few
module attribute identities. Nothing is deep-copied.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

BASELINE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "test_state_leak_baseline.json"
# Upper bound on baseline entries. Lower it when you fix a leaker; never raise it.
BASELINE_CEILING = 0

# Integration tests run real subprocesses with their own conftest guards;
# their session fixtures legitimately shape the environment.
EXCLUDED_ROOTS = ("tests/integration/", "tests/benchmarks/")

# pytest rewrites this one per phase; it is not test-owned state.
IGNORED_ENV = frozenset({"PYTEST_CURRENT_TEST"})
# ``os.environ`` keeps its entries encoded in ``_data`` (bytes on POSIX,
# upper-cased str on Windows); copying that dict is several times cheaper
# than decoding every entry through ``dict(os.environ)``.
_ENV_DATA = getattr(os.environ, "_data", None)
_IGNORED_RAW = frozenset(
    os.environ.encodekey(name) if hasattr(os.environ, "encodekey") else name for name in IGNORED_ENV
)


@dataclass(frozen=True)
class TrackedGlobal:
    """A process-global module attribute a test must leave as it found it.

    ``lazy`` marks a memo that may go from ``None`` to a resolved value (its
    owner fills it on first use) but must never change once set.
    """

    module: str
    name: str
    lazy: bool = False

    @property
    def key(self) -> str:
        return f"{self.module}.{self.name}"


TRACKED_GLOBALS: tuple[TrackedGlobal, ...] = (
    # Reset around every test by the root conftest; strict.
    TrackedGlobal("apm_cli.utils.console", "_console_stderr"),
    TrackedGlobal("apm_cli.config", "_config_cache"),
    TrackedGlobal("apm_cli.marketplace.registry", "_registry_cache"),
    # Executable lookups memoised by their owner.
    TrackedGlobal("apm_cli.utils.git_env", "_git_executable", lazy=True),
    TrackedGlobal("apm_cli.utils.git_env", "_gh_executable", lazy=True),
)


@dataclass(frozen=True)
class Snapshot:
    env: dict
    cwd: str | None
    values: dict[str, object]


def take_snapshot() -> Snapshot:
    try:
        cwd: str | None = os.getcwd()
    except OSError:
        cwd = None
    # Import tracked owners up front so a test cannot set state in a module
    # the "before" snapshot never saw.
    values = {
        tracked.key: getattr(
            sys.modules.get(tracked.module) or importlib.import_module(tracked.module),
            tracked.name,
        )
        for tracked in TRACKED_GLOBALS
    }
    return Snapshot(_env_copy(), cwd, values)


def _env_copy() -> dict:
    env = dict(_ENV_DATA) if isinstance(_ENV_DATA, dict) else dict(os.environ)
    for name in _IGNORED_RAW:
        env.pop(name, None)
    return env


def _text(value: object) -> str:
    return os.fsdecode(value) if isinstance(value, bytes) else str(value)


def _unchanged(old: object, new: object) -> bool:
    return old is new or (type(old) in (bool, str) and old == new)


def leaked_keys(before: Snapshot, after: Snapshot) -> list[str]:
    """Name every piece of tracked state that differs after the test."""
    leaks: list[str] = []
    if before.env != after.env:
        leaks.extend(
            f"env:{_text(name)}"
            for name in sorted(before.env.keys() | after.env.keys())
            if before.env.get(name) != after.env.get(name)
        )
    if before.cwd != after.cwd:
        leaks.append("cwd")
    for tracked in TRACKED_GLOBALS:
        old, new = before.values[tracked.key], after.values[tracked.key]
        if _unchanged(old, new) or (tracked.lazy and old is None):
            continue
        leaks.append(tracked.key)
    return leaks


def restore(snapshot: Snapshot) -> None:
    """Put back the snapshotted state so a leak cannot reach the next test."""
    if _env_copy() != snapshot.env:
        current = {_text(name) for name in _env_copy()}
        wanted = {_text(name): _text(value) for name, value in snapshot.env.items()}
        for name in current - wanted.keys():
            os.environ.pop(name, None)
        for name, value in wanted.items():
            if os.environ.get(name) != value:
                os.environ[name] = value
    if snapshot.cwd is not None:
        try:
            if os.getcwd() != snapshot.cwd:
                os.chdir(snapshot.cwd)
        except OSError:
            os.chdir(snapshot.cwd)
    for tracked in TRACKED_GLOBALS:
        setattr(sys.modules[tracked.module], tracked.name, snapshot.values[tracked.key])


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, frozenset[str]]:
    """Known leakers and what each may leak. The baseline may only shrink."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    leakers = payload["leakers"]
    if not isinstance(leakers, dict):
        raise ValueError(f"{path}: 'leakers' must map node ids to leaked state keys")
    return {node: frozenset(keys) for node, keys in leakers.items()}


def verdict(
    nodeid: str, leaks: Sequence[str], baseline: Mapping[str, frozenset[str]]
) -> str | None:
    """Return a failure message, or None when the test is clean or exactly baselined."""
    allowed = baseline.get(nodeid, frozenset())
    unexpected = sorted(set(leaks) - allowed)
    if unexpected:
        return (
            f"test leaked process state {unexpected}; restore it (use monkeypatch, "
            "or reset the cache at its owner) -- see tests/utils/state_leaks.py"
        )
    if nodeid in baseline and set(leaks) != allowed:
        return (
            f"stale state-leak baseline entry: {nodeid} now leaks {sorted(leaks)}, "
            f"baseline allows {sorted(allowed)}; shrink {BASELINE_PATH.name}"
        )
    return None


def applies_to(nodeid: str) -> bool:
    return not nodeid.startswith(EXCLUDED_ROOTS)
