"""Performance promise engine, layer 1: install count budgets (P-PERF-count-budgets).

Counts, not time: git spawns by verb, HTTP requests and connects, files and
bytes hashed, files written, realpath/stat/lstat calls and lockfile reads are
the same on every runner, so this is a required check. The world is the
seeded ``tree-10`` fixture behind the offline mirror (rtt 0):

- install-cold: an empty download cache and a fresh checkout;
- install-noop: the same project again, everything installed;
- install-warm: a fresh checkout with the cache the cold install filled.

Each journey's counters must stay within ``tests/perf/count_budgets.json``.
Over budget, or a counter with no row, fails with the diff; a counter well under
its budget prints a "tighten" note. Set ``APM_PERF_COUNT_BUDGETS_MEASURE=1`` to
print the measured counters instead (to write or re-measure the rows).

The engine runs through the ``apm_engine_command`` fixture (the Python CLI) so the
counting ``sitecustomize`` loads; a frozen binary would not load it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import warnings

import pytest

from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline
from tests.fixtures.perf.count_budgets import JOURNEYS, compare, flatten, load_budgets
from tests.fixtures.perf.counters import run_counted

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.xdist_group("perf-count-budgets"),
    pytest.mark.promise("P-PERF-count-budgets"),
    pytest.mark.skipif(sys.platform != "linux", reason="budgets are measured on Linux"),
]

_TREE = "tree-10"
_MEASURE = os.environ.get("APM_PERF_COUNT_BUDGETS_MEASURE") == "1"


class TightenNote(UserWarning):
    """A counter is under its budget by more than the slack."""


@pytest.fixture(scope="module")
def journey_runs(tmp_path_factory, apm_engine_command) -> dict[str, tuple]:
    """Run install cold, no-op and warm once; each journey's flattened counters."""
    work = tmp_path_factory.mktemp("perf-count-budgets")
    tree = build_tree(TREE_RECIPES[_TREE], work / "tree")
    apm = list(apm_engine_command)
    runs: dict[str, tuple] = {}
    with serve_offline(tree, work / "world") as world:
        project = work / "project"

        def checkout() -> None:
            if project.exists():
                shutil.rmtree(project)
            shutil.copytree(tree.project, project)

        def install(journey: str) -> None:
            world.reset_traffic()
            result, counts = run_counted(
                [*apm, "install"], cwd=project, env=world.env, log=work / f"{journey}.jsonl"
            )
            locked = (project / "apm.lock.yaml").is_file()
            runs[journey] = (result, locked, flatten(counts, world.traffic().as_dict()))

        world.clear_cache()
        checkout()
        install("install-cold")
        install("install-noop")
        checkout()
        install("install-warm")
    return runs


@pytest.mark.parametrize("journey", JOURNEYS)
def test_install_stays_within_its_count_budget(journey: str, journey_runs) -> None:
    result, locked, observed = journey_runs[journey]
    output = f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
    assert result.returncode == 0, f"{journey} exited {result.returncode}:\n{output}"
    assert locked, f"{journey} left no apm.lock.yaml:\n{output}"
    # Every journey talks to git and deploys from the lockfile it wrote.
    assert observed.get("fs.lockfile_reads", 0) > 0, observed
    if _MEASURE:
        pytest.fail("MEASURED " + json.dumps({journey: observed}, sort_keys=True))
    over, tighten = compare(journey, observed, load_budgets().get(journey, {}))
    for note in tighten:
        warnings.warn(note, TightenNote, stacklevel=1)
    assert not over, "count budget exceeded:\n" + "\n".join(over)


def test_cold_install_clones_and_fetches_while_noop_spawns_no_git_fetch(journey_runs) -> None:
    """The counters see the work: a cold install clones over HTTP, a no-op does not."""
    (cold_run, _, cold), (noop_run, _, noop) = (
        journey_runs["install-cold"],
        journey_runs["install-noop"],
    )
    assert (cold_run.returncode, noop_run.returncode) == (0, 0), (cold_run.stderr, noop_run.stderr)
    assert cold.get("git.clone", 0) > 0 and cold["http.requests"] > 0, cold
    assert noop.get("git.clone", 0) == 0 and noop["http.requests"] == 0, noop
