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

from tests.lifecycle.scenarios._engine_parity import (
    _DECISION,
    _WRITE_FAULT,
    BOTH,
    DECIDE_EXIT,
    DECLINES,
    FULL,
    NEEDS_ANY,
    NEEDS_DIFF,
    MissingDecision,
    Twin,
    _assert_decision,
    _converge,
    _decide,
    _raw_state,
    _remove_deployed,
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
    hook = twin.fast.root / "fault-hook"
    hook.mkdir()
    existing = twin.fast.env.get("PYTHONPATH", "")
    # Python loads only the first sitecustomize: run the world's network guard first.
    guards = [
        Path(entry) / "sitecustomize.py"
        for entry in existing.split(os.pathsep)
        if entry and (Path(entry) / "sitecustomize.py").is_file()
    ]
    assert guards, "the world's network guard sitecustomize is missing"
    guard = guards[0].read_text(encoding="utf-8")
    (hook / "sitecustomize.py").write_text(guard + "\n\n" + _WRITE_FAULT, encoding="utf-8")
    path = [str(hook), existing]
    twin.fast.env["PYTHONPATH"] = os.pathsep.join(p for p in path if p)
    twin.fast.env["APM_TEST_WRITE_FAULT_AT"] = "2"
    try:
        decision = twin.ok("install")
    finally:
        twin.fast.env.pop("APM_TEST_WRITE_FAULT_AT", None)
    _assert_decision(decision, FULL)
    assert "write failed" in decision, decision
