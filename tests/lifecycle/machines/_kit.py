"""Shared bench of the lifecycle state machines M-SOURCE and M-SCOPE-LOCK (#4096, #4097).

A ``Bench`` is one fresh world clone at one scope. ``Bench.observe`` builds the
oracle ``Observation`` (project scope as ``RowReplay.observe`` does; user scope
over HOME and APM_HOME as ``tests/integration/_gate_machine_kit.py`` does) and
still observes a world whose lockfile is a symlink or not readable YAML.
``MachineBase.step`` runs one command through ``driver.run`` and checks I1-I9
with ``oracles.check_cross_cutting``; the before and after observations and
the result stay in the returned ``Step`` for later per-obligation checks.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import hypothesis
import yaml
from hypothesis import HealthCheck, Phase, settings
from hypothesis.control import currently_in_test_context
from hypothesis.stateful import RuleBasedStateMachine

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver, receipts
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import (
    ExpectedCell,
    Observation,
    Operation,
    check_cross_cutting,
    operation_for,
)
from tests.lifecycle.rows import _PROJECT_HOME_ALLOWED, _SURFACE_GLOBS
from tests.lifecycle.snapshot import LifecycleSurfaceRoots
from tests.lifecycle.world import World, clone
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

SCOPES = ("project", "user")
LOCKFILE = "apm.lock.yaml"
# Commands the driver has no user-scope flag for (driver._USER_SCOPE_FLAGS): they
# take -g and run from the user cwd through a project-scope driver call.
_GLOBAL_ONLY = frozenset({"lock", "compile", "outdated"})
_EMPTY = b""


def machine_settings(pr_examples: int, nightly_examples: int, steps: int) -> settings:
    """PR: engine driver, derandomized; nightly: binary driver, fresh examples."""
    nightly = driver.selected_driver() == "binary"
    return settings(
        database=None,
        deadline=None,
        derandomize=not nightly,
        max_examples=nightly_examples if nightly else pr_examples,
        phases=(Phase.generate, Phase.shrink),
        print_blob=True,
        stateful_step_count=steps,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )


class WorldPool:
    """One fresh clone of the template world per Hypothesis example."""

    def __init__(self, template: World, root: Path) -> None:
        self.template = template
        self.root = root
        self._count = itertools.count()

    def fresh(self) -> World:
        world = clone(self.template, self.root / f"example-{next(self._count)}")
        world.services.reset()
        return world


@dataclass
class Bench:
    """One world at one scope: where the manifest, lockfile and deployed files live."""

    world: World
    scope: str

    def __post_init__(self) -> None:
        self.outside.mkdir(exist_ok=True)

    @property
    def manifest_dir(self) -> Path:
        return self.world.project if self.scope == "project" else self.world.apm_home

    @property
    def deploy_root(self) -> Path:
        return self.world.project if self.scope == "project" else self.world.home

    @property
    def lock_path(self) -> Path:
        return self.manifest_dir / LOCKFILE

    @property
    def outside(self) -> Path:
        """A directory outside every scope root (symlinked lockfile targets live here)."""
        return self.world.root / "outside"

    def roots(self) -> dict[str, Path]:
        roots = {"project": self.world.project, "home": self.world.home, "outside": self.outside}
        if self.scope == "user":
            roots["cwd"] = self.world.user_cwd
        return roots

    def forbidden(self) -> dict[str, tuple[str, ...]]:
        """Roots a command at this scope must never write (I5)."""
        if self.scope == "project":
            return {"home": ("*",), "outside": ("*",)}
        return {"project": ("*",), "cwd": ("*",), "outside": ("*",)}

    def allowed(self) -> dict[str, tuple[str, ...]]:
        return {"home": _PROJECT_HOME_ALLOWED} if self.scope == "project" else {}

    def manifest(self) -> dict:
        return yaml.safe_load((self.manifest_dir / "apm.yml").read_text(encoding="utf-8"))

    def write_manifest(self, data: Mapping[str, object]) -> None:
        (self.manifest_dir / "apm.yml").write_text(yaml.safe_dump(dict(data)), encoding="utf-8")

    def run(self, args: Sequence[str]) -> CommandResult:
        """Run *args* at this scope through the driver."""
        args = tuple(args)
        if self.scope == "project":
            return driver.run(self.world, args)
        if args[0] in _GLOBAL_ONLY:
            shim = SimpleNamespace(project=self.world.user_cwd, env=self.world.env)
            return driver.run(shim, (*args, "-g"))
        if args[0] == "deps":
            # ``deps`` takes the flag on the subcommand, after the subcommand name.
            return driver.run(self.world, (*args[:2], "-g", *args[2:]), scope="user")
        return driver.run(self.world, args, scope="user")

    def observe(
        self, kinds: Mapping[str, Collection[str]], targets: Collection[str]
    ) -> Observation:
        deployed: set[str] = set()
        managed: set[str] = set()
        for package, package_kinds in kinds.items():
            for kind, target in itertools.product(package_kinds, targets):
                surface = _SURFACE_GLOBS.get((kind, target))
                if surface is None:
                    continue
                pattern, marker = surface if isinstance(surface, tuple) else (surface, "")
                marker = marker.format(pkg=package)
                hits = [
                    path
                    for path in self.deploy_root.glob(pattern.format(pkg=package))
                    if path.is_file() and marker in path.read_text(encoding="utf-8")
                ]
                if hits:
                    deployed.add(f"{package}/{kind}/{target}")
                    if not marker:
                        managed.update(
                            path.relative_to(self.deploy_root).as_posix() for path in hits
                        )
        state, lock = self._state()
        if lock is None and (self.lock_path.exists() or self.lock_path.is_symlink()):
            # An unreadable lockfile attributes nothing; I1 has no ledger to compare.
            managed.clear()
        return Observation(
            state=state,
            artifacts=ArtifactSnapshotSet.capture(self.roots()),
            deployed=frozenset(deployed),
            deploy_root=self.deploy_root,
            lock=lock,
            managed_paths=frozenset(managed),
        )

    def _state(self) -> tuple[LifecycleStateSnapshot, LockFile | None]:
        cache_root = self.world.cache_root or self.world.root / "cache"
        surfaces = LifecycleSurfaceRoots(
            self.deploy_root, self.world.apm_home, cache_root, self.scope == "user"
        )
        lock = _quiet_lock(self.lock_path)
        if lock is not None and not self.lock_path.is_symlink():
            return LifecycleStateSnapshot.capture(self.manifest_dir, surfaces=surfaces), lock
        # A symlinked or unreadable lockfile: the raw bytes are the durable state.
        manifest = _read(self.manifest_dir / "apm.yml")
        raw = _read(self.lock_path)
        semantic = b"raw:" + _sha(manifest) + b":" + _sha(raw)
        state = LifecycleStateSnapshot(
            workspace_root=self.manifest_dir,
            external_roots=(),
            manifest_bytes=manifest,
            lockfile_bytes=raw,
            deployment_records=(),
            mcp_state_bytes=_EMPTY,
            lsp_state_bytes=_EMPTY,
            files=(),
            semantic_bytes=semantic,
        )
        return state, lock


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _sha(content: bytes | None) -> bytes:
    return b"-" if content is None else hashlib.sha256(content).hexdigest().encode("ascii")


def _quiet_lock(path: Path) -> LockFile | None:
    """The parsed lockfile, or None when it is absent or not a readable lockfile."""
    if not path.is_file():
        return None
    try:
        return LockFile.from_yaml(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@dataclass(frozen=True)
class KnownCell:
    """A STRICT expected failure found on main: it must fail wherever ``applies`` holds."""

    cell_id: str
    law: str
    tracking: str
    match: str
    applies: Callable[..., bool]

    @property
    def label(self) -> str:
        return f"{self.cell_id} (tracking {self.tracking})"


@dataclass
class StepModel:
    """The OracleModel view of one machine step."""

    model: ModelState
    kinds: Mapping[str, Collection[str]]
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


@dataclass(frozen=True)
class Step:
    """One checked command: its observations stay available to later obligation checks."""

    op: Operation
    before: Observation
    after: Observation
    result: CommandResult


class MachineBase(RuleBasedStateMachine):
    """Common step runner: every command goes through the driver and the I1-I9 oracle."""

    known: tuple[KnownCell, ...] = ()

    def __init__(self, bench: Bench, model: ModelState, kinds: Mapping[str, Collection[str]]):
        super().__init__()
        self.bench = bench
        self.model = model
        self.kinds = kinds
        self.visited: set[str] = set()
        self.expected_seen: set[str] = set()

    def observe(self) -> Observation:
        return self.bench.observe(self.kinds, self.model.targets)

    def visit(self, cells: Collection[str]) -> tuple[str, ...]:
        for cell in cells:
            self.visited.add(cell)
            if currently_in_test_context():
                hypothesis.event(f"cell:{cell}")
        return tuple(sorted(cells))

    def step(
        self,
        args: Sequence[str],
        *,
        exits: Collection[int],
        cells: Collection[str] = (),
        converged: bool = False,
        model: ModelState | None = None,
        **fields: object,
    ) -> Step:
        """Run *args*, assert the exit is in *exits*, then check I1-I9 for the step."""
        args = tuple(args)
        fields.setdefault("forbidden", self.bench.forbidden())
        fields.setdefault("allowed", self.bench.allowed())
        op = operation_for(args, scope=self.bench.scope, cells=self.visit(cells), **fields)
        before = self.observe()
        result = self.bench.run(args)
        after = self.observe()
        assert result.returncode in exits, (
            f"{args!r} exited {result.returncode}, model expects {sorted(exits)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
        self.check(op, before, after, result, model or self.model, converged)
        return Step(op, before, after, result)

    def check(
        self,
        op: Operation,
        before: Observation,
        after: Observation,
        result: CommandResult | None,
        model: ModelState,
        converged: bool,
    ) -> None:
        expected: dict[str, tuple[ExpectedCell, ...]] = {}
        for cell in self.known:
            if cell.applies(self, op, before, after, result):
                expected[cell.law] = (
                    *expected.get(cell.law, ()),
                    ExpectedCell(cell.label, cell.match),
                )
        view = StepModel(model, self.kinds, converged, expected, self.expected_seen)
        check_cross_cutting(before, after, result, view, op)

    def edit(self, name: str, change: Callable[[], None], *, cells: Collection[str] = ()) -> None:
        """An input edit (manifest, lockfile, network): checked as an edit-only step."""
        op = Operation(
            name,
            edit_only=True,
            scope=self.bench.scope,
            cells=self.visit(cells),
        )
        before = self.observe()
        change()
        after = self.observe()
        self.check(op, before, after, None, self.model, False)


def earned_cells(node: str, prefix: str) -> frozenset[str]:
    """Obligation cells (``<prefix>|...``) the oracle receipts of *node* carry."""
    return frozenset(
        cell
        for receipt in receipts.earned(node)
        for cell in receipt.params.get("cells", ())
        if str(cell).startswith(prefix)
    )
