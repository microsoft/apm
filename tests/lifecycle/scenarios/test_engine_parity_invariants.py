"""Install engine parity: hand-off and crash invariants of the fast engines.

PROMISE: P-INV-I7 -- nothing is skipped silently and a failed command preserves state:
a declined fast path writes nothing, decide mode never touches caches, and a fault or
SIGKILL mid-commit recovers to the full world. Oracle and cell contract in
_engine_parity.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.lifecycle import driver
from tests.lifecycle.scenarios._deploy_diff import comparable, diff
from tests.lifecycle.scenarios._engine_parity import (
    _DECISION,
    _KILL,
    _KILLED,
    _RELEASE_FAULT,
    _WRITE_FAULT,
    BOTH,
    DECIDE_EXIT,
    DECIDE_HITS,
    DECLINES,
    DIFF,
    FAST_ENV,
    FULL,
    KILL_POINTS,
    NEEDS_ANY,
    NEEDS_DIFF,
    MissingDecision,
    Twin,
    _assert_caches_untouched,
    _assert_decision,
    _cache_stats,
    _converge,
    _decide,
    _install_hook,
    _kind,
    _mcp_configs,
    _modes,
    _raw_state,
    _read_only_caches,
    _remove_deployed,
    _run_outside_contract,
    make_twin,
)
from tests.lifecycle.world import World

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I7"),
    pytest.mark.skipif(sys.platform == "win32", reason="fast engines are POSIX-only"),
]


@pytest.fixture
def twin(lifecycle_template: World, tmp_path: Path) -> Twin:
    return make_twin(lifecycle_template, tmp_path)


@pytest.mark.parametrize(
    "trigger", [pytest.param(t, marks=NEEDS_ANY.marks) for t in sorted(DECLINES)]
)
def test_declined_fast_path_writes_nothing(twin: Twin, trigger: str) -> None:
    """Hand-off invariant: with ``APM_INSTALL_FAST_PATH=decide`` the engine decides and,
    on a decline, exits ``DECIDE_EXIT`` before the full pipeline; not one byte may
    have moved by then.
    """
    keys, change, argv, ordinary = DECLINES[trigger]
    _converge(twin, *keys)
    world = twin.fast
    change(world)
    before = _raw_state(world)
    stats = _cache_stats(world)
    result = _decide(world, argv)
    evidence = result.stdout + result.stderr
    if result.returncode != DECIDE_EXIT:
        # No decide seam: the ordinary pipeline ran, and it must still behave.
        assert result.returncode == ordinary, f"exit {result.returncode}, expected {ordinary}"
        raise MissingDecision("expected a decide decision, got the full pipeline")
    decisions = _DECISION.findall(result.stderr)
    assert len(decisions) == 1, f"one decision per command, got {decisions}"
    _assert_decision(decisions[0], FULL)
    assert not result.stdout.strip(), f"a decline printed pipeline output: {evidence}"
    assert evidence.isascii(), "I6: decide output is not ASCII"
    after = _raw_state(world)
    changed = [
        f"{name}:{rel}"
        for name in before
        for rel in sorted(before[name].keys() | after[name].keys())
        if before[name].get(rel) != after[name].get(rel)
    ]
    assert not changed, f"{trigger}: the declined fast path wrote: {changed}"
    _assert_caches_untouched(stats, _cache_stats(world), trigger)


@pytest.mark.parametrize(
    "hit", [pytest.param(h, marks=DECIDE_HITS[h][3]) for h in sorted(DECIDE_HITS)]
)
def test_decide_mode_hit_leaves_caches_untouched(twin: Twin, hit: str) -> None:
    """Decide mode runs under read-only caches: a hit reads them and never writes,
    evicts, touches or creates any cache path.
    """
    keys, change, kind, _ = DECIDE_HITS[hit]
    _converge(twin, *keys)
    world = twin.fast
    change(world)
    with _read_only_caches(world):
        stats = _cache_stats(world)
        result = _decide(world, ("install",))
        after = _cache_stats(world)
    decisions = _DECISION.findall(result.stderr)
    if not decisions:
        assert result.returncode == 0, result.stdout + result.stderr
        raise MissingDecision("expected a decide decision, got the full pipeline")
    assert len(decisions) == 1, f"one decision per command, got {decisions}"
    _assert_decision(decisions[0], kind)
    assert result.returncode in (0, DECIDE_EXIT), result.stdout + result.stderr
    _assert_caches_untouched(stats, after, hit)


@NEEDS_DIFF
def test_write_fault_mid_commit_falls_back_to_an_identical_full_run(twin: Twin) -> None:
    """A fault on the commit's 2nd write: the engine declines and the full run's tree wins.

    The fault is injected through a ``sitecustomize`` on the fast world's PYTHONPATH,
    so the tree must equal the full world's (never a mix of both).
    """
    if os.environ.get("APM_LIFECYCLE_DRIVER") == "binary":
        pytest.skip("the fault hook loads only under the engine driver")
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)
    _install_hook(twin.fast, _WRITE_FAULT)
    twin.fast.env["APM_TEST_WRITE_FAULT_AT"] = "2"
    try:
        decision = twin.ok("install")
    finally:
        twin.fast.env.pop("APM_TEST_WRITE_FAULT_AT", None)
    _assert_decision(decision, FULL)
    assert "write failed" in decision, decision


@pytest.mark.parametrize(
    "point", [pytest.param(p, marks=NEEDS_DIFF.marks) for p in sorted(KILL_POINTS)]
)
def test_kill_mid_commit_then_install_equals_the_full_world(twin: Twin, point: str) -> None:
    """SIGKILL the applying engine mid-commit; the next plain install must leave the tree,
    lock, receipts and modes of a full install: no partial package or file, no staging.
    """
    if os.environ.get("APM_LIFECYCLE_DRIVER") == "binary":
        pytest.skip("the kill hook loads only under the engine driver")
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)  # warm-locked: the engine applies packages and files
    services = point == "after-first-service-write"
    if services:
        configs = _mcp_configs(twin.fast)
        assert configs, "the fixture must register an MCP server for services to rewrite"
        twin.each(lambda w: [path.unlink() for path in _mcp_configs(w)])
    _install_hook(twin.fast, _KILL)
    why = "the apply is SIGKILLed mid-commit by design; the cell asserts the kill"
    env = {FAST_ENV: "trace", **KILL_POINTS[point]}
    killed = _run_outside_contract(twin.fast, ("install",), env, why)
    if killed.returncode not in _KILLED:
        assert killed.returncode == 0, killed.stdout + killed.stderr
        raise MissingDecision(f"expected a mid-commit-kill decision, got exit {killed.returncode}")
    recovered = driver.run(twin.fast, ("install",))
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    reference = twin.invoke(twin.full, ("install",))
    assert reference.returncode == 0, reference.stdout + reference.stderr
    if services:
        assert _mcp_configs(twin.full), "the full install must rewrite the service configs"
    staging = twin.fast.project / "apm_modules" / ".apm-resolution-staging"
    assert not staging.exists(), f"staging left behind: {sorted(staging.rglob('*'))}"
    problems = diff(
        comparable(twin.fast), comparable(twin.full), twin.worlds, ("recovered", "full")
    )
    assert not problems, f"{point}: recovery differs from a full install\n" + "\n".join(problems)
    modes = [_modes(world) for world in twin.worlds]
    assert modes[0] == modes[1], f"{point}: file modes differ after recovery"


@NEEDS_DIFF
def test_output_failure_after_commit_never_runs_the_full_pipeline(twin: Twin) -> None:
    """Printing a committed apply fails (a broken pipe): the command exits non-zero, the
    full pipeline never starts on top of it, and the tree equals a full install's.
    """
    if os.environ.get("APM_LIFECYCLE_DRIVER") == "binary":
        pytest.skip("the release hook loads only under the engine driver")
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)  # warm-locked: the engine applies
    _install_hook(twin.fast, _RELEASE_FAULT)
    why = "held output release fails by design; the cell asserts the non-zero exit"
    result = _run_outside_contract(twin.fast, ("install",), {FAST_ENV: "trace"}, why)
    evidence = result.stdout + result.stderr
    if result.returncode == 0:
        raise MissingDecision("expected a diff decision, got a clean run (output never failed)")
    assert "Installing dependencies" not in result.stdout, f"the full pipeline ran: {evidence}"
    decisions = _DECISION.findall(result.stderr)
    assert len(decisions) <= 1, f"one decision at most, got {decisions}"
    assert not decisions or _kind(decisions[0]) == DIFF, decisions
    reference = twin.invoke(twin.full, ("install",))
    assert reference.returncode == 0, reference.stdout + reference.stderr
    problems = diff(comparable(twin.fast), comparable(twin.full), twin.worlds, ("fast", "full"))
    assert not problems, "the committed tree differs from a full install\n" + "\n".join(problems)
    modes = [_modes(world) for world in twin.worlds]
    assert modes[0] == modes[1], "file modes differ from a full install"
