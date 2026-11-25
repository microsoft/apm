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


class _Ctx(journeys.Context):
    """The real ``checked`` over a scripted ``run``: (returncode, trace) per call."""

    def __init__(self, *results: tuple[int, str]) -> None:
        self.is_installed = False
        self.runs = 0
        self.results = list(results) or [(0, "recorded")] + [(0, "hit")] * 9

    def installed(self) -> None:
        self.is_installed = True

    def roots(self):
        return [Path(".")]

    def run(self, _argv, _root):
        code, outcome = self.results[self.runs]
        self.runs += 1
        trace = f"{journeys._REPLAY_LINE}{outcome}\n" if outcome else ""
        return SimpleNamespace(returncode=code, stdout="", stderr=trace + "boom\n")


def test_the_replay_setup_converges_once_per_journey() -> None:
    ctx = _Ctx()

    journeys._setup_replay(ctx)
    after_first = ctx.runs
    journeys._setup_replay(ctx)
    journeys._setup_replay(ctx)

    assert after_first == 2
    assert ctx.runs == after_first


@pytest.mark.parametrize("failing", [0, 1], ids=["first-no-op", "converging-no-op"])
def test_a_failed_warm_up_install_fails_the_cell(failing: int) -> None:
    """A nonzero warm-up never reads as an older CLI without the record."""
    results = [(0, "recorded"), (0, "hit")]
    results[failing] = (1, "")
    ctx = _Ctx(*results)

    with pytest.raises(RuntimeError, match=r"failed \(1\)"):
        journeys._setup_replay(ctx)


def test_the_frozen_noop_journey_reinstalls_an_installed_project_frozen() -> None:
    """install-frozen-noop: install #2 with --frozen (#4770), on every fixture."""
    journey = journeys.JOURNEYS["install-frozen-noop"]
    ctx = SimpleNamespace(roots=lambda: [Path(".")], project=Path("."))

    assert "install-frozen-noop" in journeys.ALL_JOURNEYS
    assert journey.setup is journeys.Context.installed
    assert journey.env == journeys._REPLAY_TRACE
    assert [argv for argv, _cwd in journey.steps(ctx)] == [["install", "--frozen"]]
    assert journeys._journeys_for(_fixture("a", "b"), 0, True, [journey.name], []) == [journey.name]
