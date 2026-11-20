"""Derive the generated-obligation witnesses from the collected lifecycle tables.

A table row names its exact generated obligation id as its pytest parameter
id, so the witness of ``G|install|local-deny|hook`` is the node
``...::test_gate_change_removes_on_the_next_mutating_command[G|install|local-deny|hook]``.
A strict expected failure whose reason starts with a public issue reference
(``#<n>``) is a known-failing cell (it discharges the obligation and earns no
receipt); any other strict expected failure stays in the baseline, and so
does a row whose obligation definition awaits a ruling
(``definition_pending``), which checks its facts but earns no receipt.
Every other row becomes a required witness, which the receipt plugin then
holds to an oracle receipt for its id. Journey ids (``J|...``) come from
``journey_witnesses()`` in ``tests/lifecycle/journeys/test_journeys.py``.

Usage::

    uv run --extra dev python -m tests.lifecycle.witnesses          # check
    uv run --extra dev python -m tests.lifecycle.witnesses --write  # update
    uv run --extra dev python -m tests.lifecycle.witnesses --write <table paths>
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Mapping

import pytest

from tests.lifecycle.obligation_space import generated_obligations
from tests.lifecycle.obligations import GENERATED_PATH

TABLE_PATHS = ("tests/lifecycle/security",)
_ISSUE = re.compile(r"#(\d+)\b")


class _Collector:
    def __init__(self) -> None:
        self.items: list[pytest.Item] = []

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        self.items = list(items)


def collect(paths: Iterable[str] = TABLE_PATHS) -> list[pytest.Item]:
    collector = _Collector()
    code = pytest.main(
        ["--collect-only", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", *paths],
        plugins=[collector],
    )
    if code not in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        raise SystemExit(f"collection failed with {code}")
    return collector.items


def _pending(callspec: object) -> bool:
    """A row whose obligation definition awaits a ruling (``definition_pending``)."""
    return any(getattr(value, "definition_pending", "") for value in callspec.params.values())


def witnesses(
    items: Iterable[pytest.Item], universe: Mapping[str, object]
) -> tuple[dict[str, str], dict[str, str]]:
    """``(required, known_failing)`` for every collected row that names a generated id."""
    required: dict[str, str] = {}
    known: dict[str, str] = {}
    for item in items:
        callspec = getattr(item, "callspec", None)
        key = callspec.id if callspec is not None else None
        if key not in universe:
            continue
        assert key not in required and key not in known, f"{key} has two witnesses"
        if _pending(callspec):
            continue  # replays and checks its facts but earns no receipt yet
        mark = item.get_closest_marker("xfail")
        if mark is not None and mark.kwargs.get("strict"):
            issue = _ISSUE.match(str(mark.kwargs.get("reason", "")))
            if issue is not None:
                known[key] = f"#{issue.group(1)}"
            continue
        required[key] = item.nodeid
    return dict(sorted(required.items())), dict(sorted(known.items()))


def main(argv: list[str]) -> int:
    paths = [arg for arg in argv if not arg.startswith("--")] or list(TABLE_PATHS)
    required, known = witnesses(collect(paths), generated_obligations())
    # Journeys: one node credits many J ids, decided statically from the journey files.
    from tests.lifecycle.journeys.test_journeys import journey_witnesses

    journey_required, journey_known = journey_witnesses()
    clash = (set(required) | set(known)) & (set(journey_required) | set(journey_known))
    assert not clash, f"ids with two witnesses: {sorted(clash)}"
    required = dict(sorted({**required, **journey_required}.items()))
    known = dict(sorted({**known, **journey_known}.items()))
    data = json.loads(GENERATED_PATH.read_text(encoding="ascii"))
    current = (data.get("required", {}), data.get("known_failing", {}))
    if "--write" in argv:
        data["required"], data["known_failing"] = required, known
        GENERATED_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="ascii")
        print(f"[+] {len(required)} required, {len(known)} known failing")
        return 0
    if current != (required, known):
        print("[x] generated_obligations.json is stale; run with --write")
        return 1
    print(f"[+] up to date: {len(required)} required, {len(known)} known failing")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
