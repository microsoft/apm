"""Replay one lifecycle row through the driver and the oracle (#4093).

A row is YAML::

    id: T0/core/gate/deny-retires-hook
    promises: [P-INV-I1, ...]
    cell: {OP: install, SRC: local-path, PRIM: hook, SCOPE: project, GATE: denied}
    world: {packages: [p-skill, p-exec], targets: [copilot], scope: project}
    given: [apm install p-skill p-exec, apm approve p-exec, apm install]
    when: [apm deny p-exec, apm install, {pair: [apm audit --ci, apm install --frozen]}]
    then: {exit: 0, deployed: expected, announce: denied}

Promise ids must resolve: ``P-INV-I1``..``P-INV-I9`` or ``P-LAW-<property>``
for a ``property_catalog`` id of the lifecycle bug ledger. Package names in a command line are replaced by their ``PackageRef.spec``. A
step is a command line, ``{run: <line>, exit: <code>}`` to pin its exit code,
or ``{pair: [<audit line>, <install --frozen line>]}`` for the I3 probe.
Every step runs ``check_cross_cutting``; every exit code must fall in the
model's ``expected_exit`` unless the step pins it; ``then`` compares the
final world with ``expected_deployed`` and ``announce`` must appear in the
output of the last step (I7: nothing is skipped silently).

``known_cells`` lists STRICT expected failures found on main: each names the
law, the tracking issue, the step line it fails on and a ``match`` regex for
the violation. It must fail on exactly that step; a fix flips it to an
unexpected pass that fails the row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.model.expected import expected_deployed, expected_exit
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import (
    LEDGER_PATH,
    ORACLE_LAWS,
    ExpectedCell,
    Observation,
    Operation,
    check_cross_cutting,
    operation_for,
)
from tests.lifecycle.snapshot import LifecycleSurfaceRoots
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

# Where a fixture package's surface lands, per (kind, target) at project scope.
# Fixture convention: a package's skill is named after the package.
_SURFACE_GLOBS: Mapping[tuple[str, str], str] = {
    ("skill", "copilot"): ".agents/skills/{pkg}/SKILL.md",
    ("hook", "copilot"): ".github/hooks/{pkg}-*.json",
}
# HOME paths a project command may write: receipts and bookkeeping only.
_PROJECT_HOME_ALLOWED = (".apm/deployment-receipts", ".apm-local", ".local/state/gh", ".cache/apm")
_RECONCILING = frozenset({"install", "update"})


@dataclass(frozen=True)
class Row:
    id: str
    promises: tuple[str, ...]
    packages: tuple[str, ...]
    targets: tuple[str, ...]
    scope: str
    given: tuple[object, ...]
    when: tuple[object, ...]
    then: Mapping[str, object] = field(default_factory=dict)
    cell: Mapping[str, str] = field(default_factory=dict)
    known_cells: tuple[Mapping[str, str], ...] = ()


def promise_catalog() -> frozenset[str]:
    """Promise ids a row may name."""
    ledger = json.loads(LEDGER_PATH.read_text(encoding="ascii"))
    laws = {f"P-INV-{law}" for law in ORACLE_LAWS if law.startswith("I")}
    return frozenset(laws | {f"P-LAW-{row['id']}" for row in ledger["property_catalog"]})


def load_row(path: Path) -> Row:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    world = data["world"]
    assert data.get("promises"), f"{path}: every row names its promise ids"
    unknown = set(data["promises"]) - promise_catalog()
    assert not unknown, f"{path}: unknown promise ids {sorted(unknown)}"
    return Row(
        data["id"],
        tuple(data["promises"]),
        tuple(world["packages"]),
        tuple(world.get("targets", ("copilot",))),
        world.get("scope", "project"),
        tuple(data.get("given", ())),
        tuple(data.get("when", ())),
        data.get("then", {}),
        data.get("cell", {}),
        tuple(data.get("known_cells", ())),
    )


@dataclass
class _RowModel:
    """The OracleModel view of one step."""

    model: ModelState
    kinds: Mapping[str, frozenset[str]]
    converged: bool
    cells: Mapping[str, tuple[ExpectedCell, ...]]
    seen: set[str]

    def allowed_surfaces(self) -> frozenset[str]:
        return frozenset("/".join(cell) for cell in expected_deployed(self.model, self.kinds))

    def converged_before(self) -> bool:
        return self.converged

    def expected_cells(self, op, before, after, result) -> Mapping[str, tuple[ExpectedCell, ...]]:
        del op, before, after, result
        return self.cells

    def record_expected(self, cell_id: str) -> None:
        self.seen.add(cell_id)


class RowReplay:
    """Drive one row in one world, keeping the reference model in step."""

    def __init__(self, world, row: Row, *, entry_point: Sequence[str] | None = None) -> None:
        if row.scope != "project":
            raise NotImplementedError("row replay supports project scope so far")
        self.world = world
        self.row = row
        self.entry_point = entry_point
        self.kinds = {name: world.packages[name].kinds for name in row.packages}
        self.model = ModelState(targets=frozenset(row.targets))
        self.last: CommandResult | None = None
        self.expected_seen: set[str] = set()
        for cell in row.known_cells:
            assert re.fullmatch(r"#\d+", cell["tracking"]), cell
            assert cell["law"] in ORACLE_LAWS, cell

    def replay(self) -> None:
        self._set_targets()
        for step in (*self.row.given, *self.row.when):
            self.step(step)
        self.check_then()
        strict = {_label(cell) for cell in self.row.known_cells}
        assert self.expected_seen == strict, (
            f"strict expected failures that no longer fail: {sorted(strict - self.expected_seen)}"
        )

    def step(self, step: object) -> None:
        if isinstance(step, Mapping) and "pair" in step:
            audit_line, frozen_line = step["pair"]
            audit = self._run(audit_line, read_only=True)
            self._run(frozen_line, paired={"audit": audit.returncode})
        elif isinstance(step, Mapping):
            self._run(step["run"], pinned_exit=step.get("exit"))
        else:
            self._run(str(step))

    def check_then(self) -> None:
        then = self.row.then
        if "exit" in then:
            assert self.last is not None and self.last.returncode == then["exit"], self.last
        if then.get("deployed") == "expected":
            wanted = {"/".join(cell) for cell in expected_deployed(self.model, self.kinds)}
            observed = set(self.observe().deployed)
            assert observed == wanted, f"deployed {sorted(observed)}, model {sorted(wanted)}"
        if "announce" in then:
            assert self.last is not None
            output = " ".join(f"{self.last.stdout}\n{self.last.stderr}".split())
            assert str(then["announce"]) in output, f"I7: not announced: {then['announce']!r}"

    def observe(self) -> Observation:
        project = self.world.project
        deployed: set[str] = set()
        managed: set[str] = set()
        for package, kinds in self.kinds.items():
            for kind in kinds:
                for target in self.model.targets:
                    pattern = _SURFACE_GLOBS[(kind, target)].format(pkg=package)
                    hits = [path for path in project.glob(pattern) if path.is_file()]
                    if hits:
                        deployed.add(f"{package}/{kind}/{target}")
                        managed.update(path.relative_to(project).as_posix() for path in hits)
        cache_root = getattr(self.world, "cache_root", None) or self.world.root / "cache"
        state = LifecycleStateSnapshot.capture(
            project,
            surfaces=LifecycleSurfaceRoots(project, self.world.apm_home, cache_root, False),
        )
        return Observation(
            state=state,
            artifacts=ArtifactSnapshotSet.capture({"project": project, "home": self.world.home}),
            deployed=frozenset(deployed),
            deploy_root=project,
            lock=LockFile.read(project / "apm.lock.yaml"),
            managed_paths=frozenset(managed),
        )

    def _run(
        self,
        line: str,
        *,
        read_only: bool = False,
        paired: Mapping[str, int] | None = None,
        pinned_exit: int | None = None,
    ) -> CommandResult:
        requested = self._requested(driver.command_args(line))
        args = self._args(line)
        fields: dict[str, object] = {
            "forbidden": {"home": ("*",)},
            "allowed": {"home": _PROJECT_HOME_ALLOWED},
            "read_only": read_only,
            "paired": paired or {},
        }
        if args[0] not in _RECONCILING:
            fields["mutating"] = False
        op = operation_for(args, scope=self.row.scope, **fields)
        frozen = "FROZEN" in op.modifiers
        allowed_exit = (
            frozenset({pinned_exit})
            if pinned_exit is not None
            else expected_exit(args[0], requested, frozen=frozen)
        )
        cells: dict[str, tuple[ExpectedCell, ...]] = {}
        for cell in self.row.known_cells:
            if cell["step"] == line:
                cells[cell["law"]] = (
                    *cells.get(cell["law"], ()),
                    ExpectedCell(_label(cell), cell["match"]),
                )
        converged = self.model.converged and requested == self.model
        model = _RowModel(requested, self.kinds, converged, cells, self.expected_seen)
        before = self.observe()
        result = driver.run(self.world, args, scope=self.row.scope, entry_point=self.entry_point)
        after = self.observe()
        assert result.returncode in allowed_exit, (
            f"R: {line!r} exited {result.returncode}, model expects {sorted(allowed_exit)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
        check_cross_cutting(before, after, result, model, op)
        self._advance(op, requested, result)
        self.last = result
        return result

    def _advance(self, op: Operation, requested: ModelState, result: CommandResult) -> None:
        if result.returncode != 0:
            return
        if op.command in _RECONCILING and "FROZEN" not in op.modifiers:
            self.model = requested.installed()
        elif op.command in _RECONCILING:
            self.model = replace(requested, converged=self.model.converged)
        else:
            self.model = requested

    def _requested(self, args: tuple[str, ...]) -> ModelState:
        """The model after the user's request, before the CLI reconciles it."""
        names = [name for name in args[1:] if name in self.kinds]
        command = args[0]
        if command == "install" and names:
            return self.model.declare(*names)
        if command == "uninstall":
            return self.model.undeclare(*names)
        if command == "approve":
            return _each(self.model, names, lambda m, n: m.approve(n, self.kinds[n]))
        if command == "deny":
            return _each(self.model, names, lambda m, n: m.deny(n, self.kinds[n]))
        return self.model

    def _args(self, line: str) -> tuple[str, ...]:
        args = driver.command_args(line)
        specs = {name: self.world.packages[name].spec for name in self.kinds}
        return tuple(specs.get(arg, arg) for arg in args)

    def _set_targets(self) -> None:
        manifest = self.world.project / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["targets"] = list(self.row.targets)
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _label(cell: Mapping[str, str]) -> str:
    return f"{cell['id']} (tracking {cell['tracking']})"


def _each(model: ModelState, names, change) -> ModelState:
    for name in names:
        model = change(model, name)
    return model
