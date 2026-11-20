"""M-SCOPE-LOCK: a newer, corrupt or symlinked lockfile at project and user scope (#4097).

PROMISE: P-OWN-lockfile-generator-compatibility -- a lockfile written by a newer
APM is refused by every command that writes, replays or certifies it, and
read-only commands only warn (source: src/apm_cli/deps/lockfile_generator.py).
PROMISE: P-OWN-read-only-lockfile-path -- APM never writes a lockfile through a
symbolic link (source: src/apm_cli/deps/lockfile.py ensure_lockfile_writable).
PROMISE: P-INV-I5 / P-INV-I7 -- a refused command leaves every byte unchanged.
CELL: OP x SCOPE x LOCK (the T4 triples, ``T4_CELLS``).

The machine stamps the lockfile with a newer generator, corrupts it, or
replaces it with a symlink, then drives every lockfile-reading command: each
command that would write the lockfile must refuse (exit 1), every command must
leave project, HOME, user cwd and the symlink target byte-identical, and the
read-only commands must keep working. ``repair`` restores the good lockfile.
Every step runs I1-I9 through ``check_cross_cutting``.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Collection
from pathlib import Path

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import initialize, precondition, rule, run_state_machine_as_test

from tests.lifecycle.machines._kit import (
    SCOPES,
    Bench,
    MachineBase,
    WorldPool,
    earned_cells,
    machine_settings,
)
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.receipts import current_node

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I4",
        "P-INV-I5",
        "P-INV-I6",
        "P-INV-I7",
        "P-INV-I8",
        "P-INV-I9",
        "P-OWN-lockfile-generator-compatibility",
        "P-OWN-read-only-lockfile-path",
        "P-LAW-transaction.failed_command_preserves_state",
    ),
]

LOCK_STATES = ("consistent", "newer", "corrupt", "symlink")
BAD_LOCKS = LOCK_STATES[1:]
NEWER_VERSION = "99.0.0"
CORRUPT_LOCK = "lockfile_version: '1'\ndependencies: [\n  - {broken\n"
BASE, TOGGLE = "p-skill", "p-child"
KINDS = {BASE: frozenset({"skill"}), TOGGLE: frozenset({"skill"})}

# Commands that write, replay or certify the lockfile; ``add`` installs and
# ``remove`` uninstalls the toggle package.
WRITERS = ("install", "install-frozen", "update", "lock", "prune", "add", "remove")
MUTATING = (*WRITERS[:5], "compile", *WRITERS[5:])
READERS = ("deps-list", "deps-tree", "deps-why", "outdated", "audit", "audit-ci")
_PROJECT_ONLY = frozenset({"prune", "audit", "audit-ci"})
# The certifying reads refuse a lockfile they cannot vouch for (lockfile_generator.py).
_CERTIFIERS = frozenset({"audit", "audit-ci"})
_ARGS = {
    "install": ("install",),
    "install-frozen": ("install", "--frozen"),
    "update": ("update", "--yes"),
    "lock": ("lock",),
    "prune": ("prune",),
    "compile": ("compile",),
    "deps-list": ("deps", "list"),
    "deps-tree": ("deps", "tree"),
    "deps-why": ("deps", "why", BASE),
    "outdated": ("outdated",),
    "audit": ("audit",),
    "audit-ci": ("audit", "--ci"),
}
_LOCK_NAMED = re.compile(r"lockfile|apm\.lock\.yaml", re.IGNORECASE)


def ops_for(scope: str, names: Collection[str]) -> tuple[str, ...]:
    return tuple(op for op in names if scope == "project" or op not in _PROJECT_ONLY)


def t4_cells(scope: str) -> frozenset[str]:
    """The OP x SCOPE x LOCK obligation cells of *scope*."""
    ops = ops_for(scope, (*MUTATING, *READERS))
    return frozenset(f"T4|{op}|{scope}|{lock}" for op in ops for lock in LOCK_STATES)


T4_CELLS = t4_cells("project") | t4_cells("user")


class ScopeLockMachine(MachineBase):
    """Reference model of lockfile refusal at one scope, checked against the real CLI."""

    def __init__(self, bench: Bench) -> None:
        super().__init__(bench, ModelState(scope=bench.scope), KINDS)
        self.lock_state = "consistent"
        self.converged = False
        self.good: bytes | None = None
        self.good_converged = False

    # -- helpers ---------------------------------------------------------------

    def _args(self, op: str) -> tuple[str, ...]:
        if op in ("add", "remove"):
            verb = "install" if op == "add" else "uninstall"
            return (verb, self.bench.world.packages[TOGGLE].spec)
        return _ARGS[op]

    def exits(self, op: str) -> frozenset[int]:
        """Exit codes the model allows for *op* in the current lock state."""
        present = TOGGLE in self.model.declared
        if self.lock_state == "consistent":
            return frozenset({1}) if op == "remove" and not present else frozenset({0})
        if op in WRITERS:
            if (
                self.lock_state == "symlink"
                and op != "remove"
                and not (op == "add" and not present)
            ):
                # Only a write through the link is refused; a no-op may succeed.
                return frozenset({0, 1})
            return frozenset({1})
        if op in _CERTIFIERS:
            return frozenset({0}) if self.lock_state == "symlink" else frozenset({1})
        if op == "compile" or (self.lock_state == "corrupt" and op in ("deps-why", "outdated")):
            return frozenset({0, 1})
        return frozenset({0})

    def run_op(self, op: str) -> None:
        cell = f"T4|{op}|{self.bench.scope}|{self.lock_state}"
        bad = self.lock_state != "consistent"
        read_only = bad or op in READERS
        requested = self._requested(op)
        present = TOGGLE in self.model.declared
        converged = self.converged and not bad and op in MUTATING and op != "remove"
        converged = converged and (op != "add" or present)
        step = self.step(
            self._args(op),
            exits=self.exits(op),
            cells=(cell,),
            converged=converged,
            model=requested,
            read_only=read_only,
            mutating=op in MUTATING and not bad,
        )
        result = step.result
        if bad and result.returncode != 0 and op in READERS:
            text = f"{result.stdout}\n{result.stderr}"
            assert _LOCK_NAMED.search(text), f"{op} failed without naming the lockfile:\n{text}"
        if self.bench.lock_path.is_symlink() != (self.lock_state == "symlink"):
            raise AssertionError(f"{op} replaced or created a symlinked lockfile")
        if result.returncode == 0 and not bad and op in MUTATING:
            self.model = requested
            self.converged = True

    def _requested(self, op: str) -> ModelState:
        if op == "add":
            return self.model.declare(TOGGLE)
        if op == "remove":
            return self.model.undeclare(TOGGLE)
        return self.model

    def break_lock(self, state: str) -> None:
        """Turn the good lockfile into *state* (an input edit, not a command)."""
        if self.lock_state != "consistent":
            self.repair()
        path = self.bench.lock_path
        self.good = path.read_bytes()
        self.good_converged = self.converged

        def change() -> None:
            if state == "newer":
                text = re.sub(
                    r"^apm_version:.*$",
                    f"apm_version: {NEWER_VERSION}",
                    self.good.decode("utf-8"),
                    flags=re.MULTILINE,
                )
                path.write_text(text, encoding="utf-8")
            elif state == "corrupt":
                path.write_text(CORRUPT_LOCK, encoding="utf-8")
            else:
                target = self.bench.outside / "linked.lock.yaml"
                target.write_bytes(self.good)
                path.unlink()
                path.symlink_to(target)

        self.edit(f"lock-{state}", change)
        self.lock_state = state
        self.converged = False
        self.model = self.model.with_lock("newer-generator" if state == "newer" else "consistent")

    # -- rules (at most ten) ---------------------------------------------------

    @initialize()
    def seed(self) -> None:
        spec = self.bench.world.packages[BASE].spec
        requested = self.model.declare(BASE)
        self.step(("install", spec), exits={0}, model=requested, mutating=True)
        self.model = requested.installed()
        self.converged = True

    @rule()
    @precondition(lambda self: self.lock_state == "consistent")
    def stamp_newer(self) -> None:
        self.break_lock("newer")

    @rule()
    @precondition(lambda self: self.lock_state == "consistent")
    def corrupt(self) -> None:
        self.break_lock("corrupt")

    @rule()
    @precondition(lambda self: self.lock_state == "consistent")
    def symlink_lock(self) -> None:
        self.break_lock("symlink")

    @rule()
    @precondition(lambda self: self.lock_state != "consistent")
    def repair(self) -> None:
        path, good = self.bench.lock_path, self.good
        assert good is not None

        def change() -> None:
            path.unlink()
            path.write_bytes(good)
            shutil.rmtree(self.bench.outside)
            self.bench.outside.mkdir()

        self.edit("lock-repair", change)
        self.lock_state = "consistent"
        self.converged = self.good_converged
        self.model = self.model.with_lock("consistent")

    @rule(op=st.sampled_from(MUTATING))
    def mutate(self, op: str) -> None:
        if self.bench.scope == "user" and op in _PROJECT_ONLY:
            op = "lock"
        self.run_op(op)

    @rule(op=st.sampled_from(READERS))
    def read(self, op: str) -> None:
        if self.bench.scope == "user" and op in _PROJECT_ONLY:
            op = "deps-list"
        self.run_op(op)


def replay_spine(machine: ScopeLockMachine) -> None:
    """Visit every T4 cell of the machine's scope with a meaningful attempt."""
    scope = machine.bench.scope
    machine.seed()
    consistent = ops_for(scope, (*MUTATING[:-2], *READERS))
    for op in (*consistent, "add", "add", "remove", "remove"):
        machine.run_op(op)
    for state in BAD_LOCKS:
        # The toggle package is absent: ``add`` is a real write attempt.
        machine.break_lock(state)
        for op in ops_for(scope, (*MUTATING[:-1], *READERS)):
            machine.run_op(op)
        machine.repair()
        # The toggle package is present: ``remove`` is a real write attempt.
        machine.run_op("add")
        machine.break_lock(state)
        machine.run_op("remove")
        machine.repair()
        machine.run_op("remove")


@pytest.mark.parametrize("scope", SCOPES)
def test_scope_lock_spine_refuses_every_write_and_keeps_reads(
    lifecycle_template, tmp_path: Path, scope: str
) -> None:
    """Every OP x SCOPE x LOCK cell: writers refuse byte-identically, readers still work."""
    machine = ScopeLockMachine(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), scope))
    replay_spine(machine)
    strict = {cell.label for cell in machine.known}
    assert machine.expected_seen == strict, sorted(strict - machine.expected_seen)
    missing = t4_cells(scope) - earned_cells(current_node(), "T4|")
    assert not missing, f"T4 cells without an oracle receipt: {sorted(missing)}"


@pytest.mark.parametrize("scope", SCOPES)
def test_scope_lock_generated_sequences_keep_cross_cutting_laws(
    lifecycle_template, tmp_path: Path, scope: str
) -> None:
    """Generate and shrink lock-corruption sequences, one fresh world per example."""
    pool = WorldPool(lifecycle_template, tmp_path)
    run_state_machine_as_test(
        lambda: ScopeLockMachine(Bench(pool.fresh(), scope)),
        settings=machine_settings(pr_examples=4, nightly_examples=10, steps=8),
    )
