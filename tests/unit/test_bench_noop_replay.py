"""The repeat no-op bench cell is scheduled only where it can replay, and converges once."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.benchmarks import journeys

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-PERF-count-budgets")]


def _fixture(*roots: str) -> journeys.Fixture:
    return journeys.Fixture(name="f", template=Path("."), roots=roots)


def test_a_multi_root_fixture_never_schedules_the_replay_cell() -> None:
    wanted = ["install-noop", "install-noop-replay"]

    single = journeys._journeys_for(_fixture("."), 0, True, wanted, [])
    monorepo = journeys._journeys_for(_fixture("a", "b"), 0, True, wanted, [])

    assert single == wanted
    assert monorepo == ["install-noop"]


class _Ctx(SimpleNamespace):
    def installed(self) -> None:
        self.is_installed = True

    def roots(self):
        return ["."]

    def run(self, _argv, _root):
        self.runs += 1
        outcome = "recorded" if self.runs == 1 else "hit"
        return SimpleNamespace(stderr=f"{journeys._REPLAY_LINE}{outcome}\n")


def test_the_replay_setup_converges_once_per_journey() -> None:
    ctx = _Ctx(is_installed=False, runs=0)

    journeys._setup_replay(ctx)
    after_first = ctx.runs
    journeys._setup_replay(ctx)
    journeys._setup_replay(ctx)

    assert after_first == 2
    assert ctx.runs == after_first
