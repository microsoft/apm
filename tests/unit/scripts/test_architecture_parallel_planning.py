"""Unit contracts for the architecture linter's parallel bucket planning.

Pure functions of `scripts/architecture_linter/parallel.py`: the default worker
count, worker-count precedence, and bucket coverage of every rule group.
"""

from __future__ import annotations

import pytest

from scripts.architecture_linter import parallel, runner

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-architecture-linter-performance"),
]


@pytest.mark.parametrize(
    ("cpus", "expected"), [(None, 1), (1, 1), (2, 1), (3, 1), (4, 2), (8, 4), (64, 4)]
)
def test_default_workers_use_half_the_cpus_capped_at_four(
    monkeypatch: pytest.MonkeyPatch, cpus: int | None, expected: int
) -> None:
    monkeypatch.setattr(parallel.os, "cpu_count", lambda: cpus)

    assert parallel.resolve_workers(None, {}) == expected


def test_worker_count_precedence_is_flag_env_then_pytest() -> None:
    pytest_env = {"PYTEST_CURRENT_TEST": "t", "PYTEST_XDIST_WORKER": "gw0"}

    assert parallel.resolve_workers(None, pytest_env) == 1
    assert parallel.resolve_workers(None, {**pytest_env, parallel.WORKERS_ENV: "3"}) == 3
    assert parallel.resolve_workers(5, {parallel.WORKERS_ENV: "3"}) == 5
    assert parallel.resolve_workers(99, {}) == len(runner.GROUP_MODULE_NAMES)


@pytest.mark.parametrize("workers", range(1, 9))
def test_buckets_cover_every_group_exactly_once(workers: int) -> None:
    buckets = parallel.plan_buckets(workers)

    flat = [group for bucket in buckets for group in bucket]
    assert sorted(flat) == sorted(runner.GROUP_MODULE_NAMES)
    assert len(buckets) == min(workers, len(runner.GROUP_MODULE_NAMES))
