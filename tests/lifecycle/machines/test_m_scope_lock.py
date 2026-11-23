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

import json
import re
import shutil
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import initialize, precondition, rule, run_state_machine_as_test

from tests.lifecycle.known import KnownFailure
from tests.lifecycle.machines._kit import (
    SCOPES,
    Bench,
    KnownCell,
    MachineBase,
    Step,
    WorldPool,
    earned_cells,
    machine_settings,
)
from tests.lifecycle.model.expected import expected_exit
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, Observation, check_obligation
from tests.lifecycle.receipts import current_node
from tests.lifecycle.world.services import REGISTRY_SERVER

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


KNOWN_CELLS: tuple[KnownCell, ...] = ()
# T4 cells whose oracle fails on main -> their known failure (generator: known-failing).
KNOWN_T4: dict[str, KnownFailure] = {}


def ops_for(scope: str, names: Collection[str]) -> tuple[str, ...]:
    return tuple(op for op in names if scope == "project" or op not in _PROJECT_ONLY)


def t4_cells(scope: str) -> frozenset[str]:
    """The OP x SCOPE x LOCK obligation cells of *scope*."""
    ops = ops_for(scope, (*MUTATING, *READERS))
    return frozenset(f"T4|{op}|{scope}|{lock}" for op in ops for lock in LOCK_STATES)


T4_CELLS = t4_cells("project") | t4_cells("user")


class ScopeLockMachine(MachineBase):
    """Reference model of lockfile refusal at one scope, checked against the real CLI."""

    known = KNOWN_CELLS

    def __init__(self, bench: Bench) -> None:
        super().__init__(bench, ModelState(scope=bench.scope), KINDS)
        self.lock_state = "consistent"
        self.converged = False
        self.good: bytes | None = None
        self.good_converged = False
        self.current = ""

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
        self.current = op
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
        self.credit(cell, step, bad)
        if bad and result.returncode != 0 and op in READERS:
            text = f"{result.stdout}\n{result.stderr}"
            assert _LOCK_NAMED.search(text), f"{op} failed without naming the lockfile:\n{text}"
        if self.bench.lock_path.is_symlink() != (self.lock_state == "symlink"):
            raise AssertionError(f"{op} replaced or created a symlinked lockfile")
        if result.returncode == 0 and not bad and op in MUTATING:
            self.model = requested
            self.converged = True

    def durable(self) -> dict[str, tuple[str, ...]]:
        """Root id -> durable prefixes: manifest, lockfile, packages, deployed files."""
        if self.bench.scope == "project":
            project = ("apm.yml", "apm.lock.yaml", "apm_modules", ".agents", ".github")
            return {"project": project, "outside": ("*",)}
        home = (".apm/apm.yml", ".apm/apm.lock.yaml", ".apm/apm_modules", ".agents", ".copilot")
        return {"home": home, "project": ("*",), "outside": ("*",)}

    def credit(self, cell: str, step: Step, bad: bool) -> None:
        """Credit the T4 cell: a bad lockfile leaves every durable byte unchanged."""
        unchanged = self.durable() if bad or self.current in READERS else {"outside": ("*",)}
        check_obligation(
            cell,
            strength="e2e-evidence",
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=Evidence(unchanged=unchanged, exit=self.exits(self.current)),
            scope=self.bench.scope,
        )

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


# -- drift: a stale or absent lockfile, and the side-door commands -------------

DRIFT_STATES = ("stale", "absent")
# Commands outside the lockfile pipeline: a verdict, install --mcp, a local
# bundle and a catalog read. ``deny`` names p-exec (installed, gated) and the
# bundle is p-lsp's (its LSP server stays parked: nothing new deploys).
SIDE_DOORS = ("deny", "mcp-install", "bundle", "marketplace")
_SIDE_PROJECT_ONLY = frozenset({"marketplace"})
# OP x SCOPE x LOCK cells the lockfile spine above leaves open, per scope and state.
_DRIFT_PLAN: dict[str, dict[str, tuple[str, ...]]] = {
    "project": {
        "stale": ("install", "update", "lock", "prune", "compile", "audit", "remove", *SIDE_DOORS),
        "absent": ("remove", *SIDE_DOORS),
        "newer": SIDE_DOORS,
        "consistent": ("bundle", "marketplace"),
    },
    "user": {
        "stale": ("install", "update", "lock", "compile", "remove", *SIDE_DOORS[:3]),
        "absent": ("install", "update", "lock", "compile", "remove", *SIDE_DOORS[:3]),
        "newer": SIDE_DOORS[:3],
        "consistent": SIDE_DOORS[:3],
    },
}
# Exit of each command per lock state where it is not 0 (observed on the CLI).
_DRIFT_EXITS: dict[tuple[str, str], frozenset[int]] = {
    ("newer", "mcp-install"): frozenset({1}),
    ("newer", "bundle"): frozenset({1}),
    # A verdict writes the approval store, never the lockfile; without a
    # lockfile nothing is installed, so the verdict matches no package (#4154).
    ("absent", "deny"): frozenset({1}),
}
_SIDE_ARGS = {
    "mcp-install": ("install", "--mcp", REGISTRY_SERVER),
    "marketplace": ("marketplace", "list"),
}


# What each command acts on, as factor values (generator/witnesses.py credits
# the LOCK pairs they imply). The lockfile spines run p-skill and p-child from
# local paths into one copilot target, online, with no executable gate level;
# ``deny`` names p-exec, the bundle carries p-lsp's parked LSP server and
# install --mcp adds a registry server.
_SKILL_CONTEXT = {
    "SRC": "local-path",
    "PRIM": "skill",
    "GATE": "none",
    "TGT": "single-unchanged",
    "NET": "online",
}
_SIDE_CONTEXT: dict[str, dict[str, str]] = {
    "deny": {"SRC": "local-path"},
    "mcp-install": {"SRC": "mcp-registry", "PRIM": "mcp"},
    "bundle": {"SRC": "local-bundle", "PRIM": "lsp", "GATE": "unapproved-exec"},
    "marketplace": {},
}


def lock_context(op: str, lock: str) -> dict[str, str]:
    """Factor values a lockfile-spine step of *op* proves beside OP, SCOPE and LOCK."""
    if op in _SIDE_CONTEXT:
        return dict(_SIDE_CONTEXT[op])
    if lock in ("corrupt", "symlink") or op in READERS[:4]:
        return {}
    # A deleted lockfile leaves the deployed skill with unknown provenance.
    deployed = "present-provenance-unknown" if lock == "absent" else "clean"
    return {**_SKILL_CONTEXT, "DEPLOYED": deployed}


def drift_cells(scope: str) -> frozenset[str]:
    """The T4 cells the drift spine credits at *scope*."""
    return frozenset(
        f"T4|{op}|{scope}|{state}" for state, ops in _DRIFT_PLAN[scope].items() for op in ops
    )


DRIFT_KNOWN_CELLS: tuple[KnownCell, ...] = ()


class DriftMachine(ScopeLockMachine):
    """The lockfile machine with drifted lockfiles and the side-door commands."""

    known = DRIFT_KNOWN_CELLS
    # Commands that resolve and deploy the declared set, so they record (adopt)
    # what is deployed; ``lock`` and the side doors write a lockfile without it.
    ADOPTING = frozenset({"install", "update", "add"})

    def __init__(self, bench: Bench) -> None:
        super().__init__(bench)
        # True after the lockfile was deleted: deployed files have unknown
        # provenance (DEPLOYED=present-provenance-unknown) until a resolving
        # command records them again, so I1 has nothing to compare them with.
        self.unknown_provenance = False

    def observe(self) -> Observation:
        observation = super().observe()
        if self.unknown_provenance:
            return replace(observation, managed_paths=frozenset())
        return observation

    def _args(self, op: str) -> tuple[str, ...]:
        packages = self.bench.world.packages
        if op == "deny":
            return ("deny", packages["p-exec"].spec)
        if op == "bundle":
            return ("install", packages["p-lsp:bundle"].spec)
        return _SIDE_ARGS.get(op) or super()._args(op)

    def exits(self, op: str) -> frozenset[int]:
        if self.lock_state in ("consistent", "newer") and op not in SIDE_DOORS:
            return super().exits(op)
        if op == "remove":
            # Uninstalling the toggle refuses when the manifest does not declare it.
            return frozenset({0}) if TOGGLE in self.model.declared else frozenset({1})
        return _DRIFT_EXITS.get((self.lock_state, op), frozenset({0}))

    def setup(self) -> None:
        """Seed p-skill, then install p-exec (its executables stay parked)."""
        self.seed()
        spec = self.bench.world.packages["p-exec"].spec
        self.step(("install", spec), exits={0}, mutating=True)

    def drift(self, state: str) -> None:
        """Make the lockfile *state* again before one command (an input edit)."""
        path = self.bench.lock_path
        if state == "absent":
            if path.exists():
                self.edit("lock-absent", path.unlink)
                self.unknown_provenance = True
            self.lock_state = "absent"
            self.model = self.model.with_lock("absent")
            return
        in_lock = TOGGLE in path.read_text(encoding="utf-8")
        if (TOGGLE in self.model.declared) == in_lock:
            # Flip the toggle in the manifest so it disagrees with the lockfile.
            spec = self.bench.world.packages[TOGGLE].spec

            def change() -> None:
                data = self.bench.manifest()
                entries = data.setdefault("dependencies", {}).get("apm") or []
                data["dependencies"]["apm"] = [e for e in entries if e != spec]
                if not in_lock:
                    data["dependencies"]["apm"].append(spec)
                self.bench.write_manifest(data)

            self.edit("lock-stale", change)
            self.model = self.model.undeclare(TOGGLE) if in_lock else self.model.declare(TOGGLE)
        self.lock_state = "stale"
        self.model = self.model.with_lock("stale")

    def run_drift(self, state: str, op: str) -> None:
        cell = f"T4|{op}|{self.bench.scope}|{state}"
        if state in DRIFT_STATES:
            if self.lock_state in BAD_LOCKS:
                self.repair()
            self.drift(state)
        elif self.lock_state != state:
            if self.lock_state != "consistent":
                self.repair()
            if state != "consistent":
                self.break_lock(state)
        self.current = op
        exits = self.exits(op)
        requested = self._requested(op)
        step = self.step(
            self._args(op),
            exits=exits,
            cells=(cell,),
            converged=False,
            model=requested,
            mutating=op in MUTATING and state != "newer",
        )
        refused = step.result.returncode != 0
        if not refused:
            # Outcome checks run before the credit: a receipt needs them to hold.
            self.check_side_door(op)
            if op in self.ADOPTING:
                self.check_reconciled(step, requested)
        unchanged = self.durable() if refused else {"outside": ("*",)}
        check_obligation(
            cell,
            strength="e2e-evidence",
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=Evidence(unchanged=unchanged, exit=exits),
            scope=self.bench.scope,
        )
        if not refused and op in MUTATING:
            self.model = requested
        if not refused and op in self.ADOPTING:
            self.unknown_provenance = False
        if not refused and self.bench.lock_path.is_file() and state in DRIFT_STATES:
            self.lock_state = "consistent"

    def check_reconciled(self, step: Step, requested: ModelState) -> None:
        """A resolving command rewrote a readable lockfile that matches the manifest.

        Every declared package has a lock row, no row is undeclared, and the
        lock records each declared skill it deployed (adopting what a deleted
        lockfile left with unknown provenance).
        """
        lock = step.after.lock
        assert lock is not None, "a resolving command left no readable lockfile"
        declared = {str(entry) for entry in _declared(self.bench.manifest())}
        rows = {str(dep.local_path) for dep in lock.get_package_dependencies()}
        assert rows == declared, f"lock rows {sorted(rows)} != manifest {sorted(declared)}"
        recorded = set(lock.local_deployed_file_hashes)
        for dep in lock.get_package_dependencies():
            recorded.update(dep.deployed_file_hashes)
        for package in requested.declared:
            skill = f".agents/skills/{package}/SKILL.md"
            if (self.bench.deploy_root / skill).is_file():
                assert skill in recorded, f"the lock does not record {skill}"

    def check_side_door(self, op: str) -> None:
        """What I1-I9 cannot see of an admitted side door: its own outcome."""
        root, scope = self.bench.deploy_root, self.bench.scope
        if op == "mcp-install":
            config = root / (".mcp.json" if scope == "project" else ".copilot/mcp-config.json")
            servers = json.loads(config.read_text(encoding="utf-8")).get("mcpServers") or {}
            short = REGISTRY_SERVER.rsplit("/", 1)[-1]
            assert {REGISTRY_SERVER, short} & set(servers), f"{short} not configured: {servers}"
            declared = str((self.bench.manifest().get("dependencies") or {}).get("mcp"))
            assert REGISTRY_SERVER in declared, f"{REGISTRY_SERVER} not declared: {declared}"
        if op == "bundle":
            # p-lsp's server stays parked: nothing approved it.
            config = root / (
                ".github/lsp.json" if scope == "project" else ".copilot/lsp-config.json"
            )
            text = config.read_text(encoding="utf-8") if config.is_file() else ""
            assert "p-lsp-server" not in text, f"a parked LSP server was configured: {text}"
            # The durable install state of a bundle: its exact-content approval
            # key and the LSP type it parked (install/local_bundle_pending.py).
            record = self.bench.manifest_dir / "apm_modules" / ".local-bundle-pending.json"
            assert record.is_file(), "bundle install recorded no pending executables"
            bundles = json.loads(record.read_text(encoding="utf-8")).get("bundles") or {}
            assert any(
                str(entry.get("approval_key", "")).startswith("p-lsp#")
                and "lsp" in entry.get("exec_types", ())
                for entry in bundles.values()
            ), f"p-lsp's parked LSP server is not pending approval: {bundles}"


def _declared(manifest: dict) -> list:
    return list((manifest.get("dependencies") or {}).get("apm") or [])


def replay_drift(machine: DriftMachine) -> None:
    """Visit every drift cell of the machine's scope."""
    machine.setup()
    plan = _DRIFT_PLAN[machine.bench.scope]
    # A stale lockfile needs one on disk: the absent state comes last.
    for state in ("consistent", "newer", "stale", "absent"):
        for op in plan[state]:
            machine.run_drift(state, op)


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
def test_scope_lock_drift_spine_credits_stale_absent_and_side_doors(
    lifecycle_template, tmp_path: Path, scope: str
) -> None:
    """A stale or absent lockfile reconciles; side-door commands keep I1-I9 at every lock."""
    machine = DriftMachine(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), scope))
    replay_drift(machine)
    strict = {cell.label for cell in machine.known if scope == "project"}
    assert machine.expected_seen == strict, sorted(strict ^ machine.expected_seen)
    missing = drift_cells(scope) - earned_cells(current_node(), "T4|")
    assert not missing, f"drift cells without an oracle receipt: {sorted(missing)}"
    # No drifted lockfile cost the user the declared skill.
    assert (machine.bench.deploy_root / ".agents/skills" / BASE / "SKILL.md").exists()


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


@pytest.mark.parametrize("command", ["install", "update"])
@pytest.mark.parametrize("frozen", [False, True])
def test_model_predicts_refusal_on_a_newer_generator_lock(command: str, frozen: bool) -> None:
    model = ModelState(declared=frozenset({TOGGLE})).with_lock("newer-generator")
    assert expected_exit(command, model, frozen=frozen) == frozenset({1})
