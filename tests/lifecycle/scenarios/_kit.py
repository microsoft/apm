"""Shared kit of the lifecycle scenario suites (#4093).

A ``Scenario`` drives one world at one scope through ``driver.run`` and keeps
the reference model (``tests/lifecycle/model``) in step, following the
``RowReplay`` pattern of ``tests/lifecycle/rows.py``:

- every step observes durable state before and after, runs the command,
  checks the exit code against the model (or a pinned code), and applies
  the cross-cutting oracle I1-I9 (``check_cross_cutting``), which is the
  only source of receipts;
- ``Step.changed`` is the exact set of durable paths the step changed, as
  ``"<root>:<path>"`` over the snapshot roots (project, home, and apm-home
  when APM_HOME lives outside HOME); ``assert_changes`` compares it with a
  set of fnmatch patterns both ways (nothing missing, nothing extra);
- a strict known cell (a defect on main) is either a law-level
  ``ExpectedCell`` on one step (``known={law: Known(...)}``; law ``R``
  expects the exit code to differ from the model's) or a block
  that must keep failing (``known_defect``); a fix turns it into an
  unexpected pass that fails the scenario.

Package names in a command line are replaced by their spec, as in rows.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.generator.witnesses import implied
from tests.lifecycle.model.expected import expected_deployed, expected_exit, expected_lock_rows
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import (
    Evidence,
    ExpectedCell,
    Observation,
    Operation,
    check_cross_cutting,
    check_obligation,
    operation_for,
    user_kept_cells,
)
from tests.lifecycle.snapshot import LifecycleSurfaceRoots
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

# Where a package's surface lands, per (scope, kind, target): a glob relative
# to the deploy root, or (glob, marker) for a shared file that must name the
# package's entry. Same world conventions as rows.py.
SURFACE_GLOBS: Mapping[tuple[str, str, str], str | tuple[str, str]] = {
    ("project", "skill", "copilot"): ".agents/skills/{pkg}/SKILL.md",
    ("project", "hook", "copilot"): ".github/hooks/{pkg}-*.json",
    ("project", "mcp", "copilot"): (".mcp.json", '"{pkg}-mcp"'),
    ("project", "lsp", "copilot"): (".github/lsp.json", '"{pkg}-server"'),
    ("project", "canvas", "copilot"): ".github/extensions/{pkg}/extension.mjs",
    ("project", "skill", "claude"): ".claude/skills/{pkg}/SKILL.md",
    ("user", "skill", "copilot"): ".agents/skills/{pkg}/SKILL.md",
    ("user", "hook", "copilot"): ".copilot/hooks/{pkg}-*.json",
    ("user", "mcp", "copilot"): (".copilot/mcp-config.json", '"{pkg}-mcp"'),
}
# HOME paths a project command may write: receipts and bookkeeping only.
PROJECT_HOME_ALLOWED = (".apm/deployment-receipts", ".apm-local", ".local/state/gh", ".cache/apm")
_RECONCILING = frozenset({"install", "update"})
_OUTPUT_LAWS = frozenset({"I6", "I8", "I9"})
_SELECTING = frozenset({"install", "uninstall", "approve", "deny", "update"})


@dataclass(frozen=True)
class Known:
    """A strict expected failure of one law on one step, tracked by an issue."""

    tracking: str
    match: str

    def __post_init__(self) -> None:
        assert re.fullmatch(r"#\d+", self.tracking), self.tracking


@dataclass
class StepModel:
    """The ``OracleModel`` view of one scenario step."""

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


@dataclass(frozen=True)
class Step:
    """One driven command: its result, the observations and the snapshots around it."""

    line: str
    result: CommandResult
    before: Observation
    after: Observation

    @property
    def changed(self) -> frozenset[str]:
        return changed_paths(self.before.artifacts, self.after.artifacts)

    @property
    def output(self) -> str:
        return " ".join(f"{self.result.stdout}\n{self.result.stderr}".split())


def changed_paths(before: ArtifactSnapshotSet, after: ArtifactSnapshotSet) -> frozenset[str]:
    """Every added, removed or changed path, as ``<root>:<path>``, without bookkeeping."""
    changed: set[str] = set()
    for root_id, snapshot in before.snapshots:
        diff = snapshot.diff(after.snapshot(root_id))
        for path in diff.added | diff.removed | diff.changed:
            if not _bookkeeping(path):
                changed.add(f"{root_id}:{path}")
    return frozenset(changed)


_BOOKKEEPING = (
    ".apm-lifecycle.lock",
    "last_version_check",
    "device-id",
)
_BOOKKEEPING_TREES = (
    "home:.apm/deployment-receipts",
    "home:.cache",
    "home:.local",
    "home:.apm-local",
    "apm-home:deployment-receipts",
)


def _bookkeeping(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in _BOOKKEEPING


def assert_changes(changed: Iterable[str], patterns: Iterable[str], *, label: str = "") -> None:
    """Every changed path matches a pattern and every pattern matches a changed path.

    Paths under ``_BOOKKEEPING_TREES`` (receipts, version-check caches) are
    ignored unless a pattern names them, and so is a directory whose change
    is implied by a changed path below it.
    """
    changed = set(changed)
    patterns = set(patterns)
    durable = {
        path
        for path in changed
        if not any(path == tree or path.startswith(tree + "/") for tree in _BOOKKEEPING_TREES)
        or any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    }
    # A directory that only holds other changed paths is implied by them; an
    # empty directory added or removed on its own still counts.
    durable = {path for path in durable if not any(o.startswith(path + "/") for o in durable)}
    extra = sorted(p for p in durable if not any(fnmatch.fnmatchcase(p, pat) for pat in patterns))
    missing = sorted(
        pat for pat in patterns if not any(fnmatch.fnmatchcase(p, pat) for p in durable)
    )
    assert not extra and not missing, (
        f"{label}: changed set differs\n  unexpected: {extra}\n  missing: {missing}"
    )


@contextmanager
def known_defect(tracking: str, match: str) -> Iterator[None]:
    """A block that must keep failing with *match* until *tracking* is fixed (strict)."""
    assert re.fullmatch(r"#\d+", tracking), tracking
    try:
        yield
    except AssertionError as exc:
        assert re.search(match, str(exc)), f"{tracking}: failed differently: {exc}"
        return
    pytest.fail(f"strict known cell {tracking} no longer fails; remove the expectation")


class Scenario:
    """Drive one world at one scope; keep the model in step; apply I1-I9 per step."""

    def __init__(
        self,
        world,
        packages: Mapping[str, str] | Iterable[str] = (),
        *,
        kinds: Mapping[str, Iterable[str]] | None = None,
        scope: str = "project",
        targets: Sequence[str] = ("copilot",),
        policy: str = "none",
        requires: Mapping[str, Iterable[str]] | None = None,
        extra_roots: Mapping[str, Path] | None = None,
    ) -> None:
        """*packages* maps a model name to its spec (or names world packages).

        *requires* maps a package to the packages it depends on, so the model
        declares the transitive closure of what the user installs.
        *extra_roots* adds snapshot roots (for example the cache) to every step.
        """
        self.world = world
        self.scope = scope
        if not isinstance(packages, Mapping):
            packages = {name: world.packages[name].spec for name in packages}
        self.specs: dict[str, str] = dict(packages)
        kinds = dict(kinds or {})
        self.kinds: dict[str, frozenset[str]] = {
            name: frozenset(kinds.get(name) or world.packages[name.split(":")[0]].kinds)
            for name in self.specs
        }
        self.extra_roots = dict(extra_roots or {})
        self.requires = {name: tuple(deps) for name, deps in (requires or {}).items()}
        self.roots: frozenset[str] = frozenset()
        self.model = ModelState(targets=frozenset(targets), scope=scope, policy=policy)
        self.expected_seen: set[str] = set()
        self.expected_all: set[str] = set()
        # I1-X2: deploy-root-relative path -> sha256 of the bytes the user wrote there.
        self.user_bytes: dict[str, str] = {}
        self.steps: list[Step] = []
        self.set_targets(*targets)

    # -- world -----------------------------------------------------------------

    @property
    def deploy_root(self) -> Path:
        return self.world.home if self.scope == "user" else self.world.project

    @property
    def manifest_root(self) -> Path:
        return self.world.apm_home if self.scope == "user" else self.world.project

    @property
    def lock_path(self) -> Path:
        return self.manifest_root / "apm.lock.yaml"

    def snapshot_roots(self) -> dict[str, Path]:
        roots = {"project": self.world.project, "home": self.world.home}
        if not self.world.apm_home.resolve().is_relative_to(self.world.home.resolve()):
            roots["apm-home"] = self.world.apm_home
        return {**roots, **self.extra_roots}

    def set_targets(self, *targets: str) -> None:
        """Declare *targets* in apm.yml (an input edit; bytes kept when already declared)."""
        manifest = self.manifest_root / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if data.get("targets") != list(targets):
            data["targets"] = list(targets)
            manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
        self.model = self.model.with_targets(*targets)

    def adopt(self, *names: str, lock: str = "consistent") -> None:
        """The world already declares *names* (a checked-out project) with a *lock* state."""
        self.roots |= set(names)
        self.model = self._declared(self.roots).with_lock(lock)

    def source_moved(self) -> None:
        """An input outside the CLI moved (a new tag, an edited cache): not converged."""
        self.model = replace(self.model, converged=False)

    def user_write(self, relative: str, content: str) -> str:
        """Write the user's own bytes at *relative* under the deploy root (an input edit).

        An edited deployed file, or a user file at a package's path: I1-X2
        exempts it from lock==disk while it holds exactly these bytes, and
        fails any step that changes or removes them. An input edit: the model
        is no longer converged.
        """
        path = self.deploy_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("ascii")
        path.write_bytes(data)
        self.user_bytes[relative] = hashlib.sha256(data).hexdigest()
        self.source_moved()
        return relative

    def release_user_write(self, relative: str) -> None:
        """The user lets APM replace *relative* (``install --force``): I1-X2 stops guarding it."""
        del self.user_bytes[relative]
        self.source_moved()

    def drop_from_manifest(self, *names: str) -> None:
        """Edit apm.yml by hand: remove *names* from ``dependencies.apm`` (an input edit)."""
        manifest = self.manifest_root / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        specs = {self.specs[name] for name in names}
        entries = data["dependencies"]["apm"]
        kept = [entry for entry in entries if _entry_spec(entry) not in specs]
        assert len(kept) == len(entries) - len(specs), f"{sorted(specs)} not all in {entries}"
        data["dependencies"]["apm"] = kept
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
        self.roots -= set(names)
        self.model = self._declared(self.roots)

    def lock(self) -> LockFile | None:
        return LockFile.read(self.lock_path)

    def lock_names(self) -> frozenset[str]:
        """Repo URLs (or local paths) of every package row in the lockfile."""
        lock = self.lock()
        if lock is None:
            return frozenset()
        return frozenset(dep.repo_url for dep in lock.get_package_dependencies())

    # -- observation -----------------------------------------------------------

    def deployed(self) -> tuple[frozenset[str], frozenset[str], dict[str, frozenset[str]]]:
        """(``pkg/kind/target`` cells found, package-owned paths, each cell's paths)."""
        root = self.deploy_root
        deployed: set[str] = set()
        managed: set[str] = set()
        cell_paths: dict[str, frozenset[str]] = {}
        users = self._user_files()
        for package, kinds in self.kinds.items():
            for kind in kinds:
                for target in self.model.targets:
                    surface = SURFACE_GLOBS.get((self.scope, kind, target))
                    if surface is None:
                        continue
                    pattern, marker = surface if isinstance(surface, tuple) else (surface, "")
                    name = package.split(":")[0]
                    marker = marker.format(pkg=name)
                    hits = [
                        path
                        for path in root.glob(pattern.format(pkg=name))
                        if path.is_file()
                        and marker in path.read_text(encoding="utf-8")
                        and path.relative_to(root).as_posix() not in users
                    ]
                    if hits:
                        cell = f"{package}/{kind}/{target}"
                        deployed.add(cell)
                        found = frozenset(path.relative_to(root).as_posix() for path in hits)
                        cell_paths[cell] = found
                        if not marker:
                            managed.update(found)
        return frozenset(deployed), frozenset(managed), cell_paths

    def _user_files(self) -> frozenset[str]:
        """I1-X2: declared user writes the lock does not record: the user's files.

        An edited file the lock still records stays a deployment of its package.
        """
        lock = self.lock()
        recorded = (
            {path for dep in lock.get_package_dependencies() for path in dep.deployed_files}
            if lock is not None
            else set()
        )
        root = self.deploy_root
        return frozenset(
            relative
            for relative, digest in self.user_bytes.items()
            if relative not in recorded
            and (root / relative).is_file()
            and hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
        )

    def observe(self) -> Observation:
        deployed, managed, cell_paths = self.deployed()
        cache_root = getattr(self.world, "cache_root", None) or self.world.root / "cache"
        state = LifecycleStateSnapshot.capture(
            self.manifest_root,
            surfaces=LifecycleSurfaceRoots(
                self.deploy_root, self.world.apm_home, cache_root, self.scope == "user"
            ),
        )
        return Observation(
            state=state,
            artifacts=ArtifactSnapshotSet.capture(self.snapshot_roots()),
            deployed=deployed,
            deploy_root=self.deploy_root,
            lock=self.lock(),
            managed_paths=managed,
            user_bytes=dict(self.user_bytes),
            cell_paths=cell_paths,
        )

    def snapshot(self) -> ArtifactSnapshotSet:
        return ArtifactSnapshotSet.capture(self.snapshot_roots())

    def expected_cells(self) -> frozenset[str]:
        """``pkg/kind/target`` cells the model allows, limited to observable surfaces."""
        return frozenset(
            "/".join(cell)
            for cell in expected_deployed(self.model, self.kinds)
            if (self.scope, cell[1], cell[2]) in SURFACE_GLOBS
        )

    def assert_deployed(self) -> None:
        observed, _, _ = self.deployed()
        wanted = self.expected_cells()
        assert observed == wanted, f"deployed {sorted(observed)}, model {sorted(wanted)}"

    # -- steps -------------------------------------------------------------------

    def run(
        self,
        line: str | Sequence[str],
        *,
        exit: int | None = None,
        read_only: bool = False,
        paired: Mapping[str, int] | None = None,
        known: Mapping[str, Known] | None = None,
        requested: ModelState | None = None,
        credit: Iterable[Mapping[str, str]] = (),
        keep: Mapping[str, Iterable[str]] | None = None,
        refusal: bool = False,
        says: Iterable[str] = (),
        **fields: object,
    ) -> Step:
        """Run one command line through the driver and apply I1-I9.

        *exit* pins the exit code; otherwise the model's ``expected_exit``
        holds. *requested* overrides the model after the user's request.
        *credit* lists the factor assignments the step proves; a successful
        step with no strict known cell credits the generated scenario-lane
        obligations they imply (``credit_step``). *keep* names paths per
        root that must be present and byte-unchanged across the step.
        *refusal* credits a declared refusal (``EXPECTED_REFUSALS``) instead.
        *says* lists patterns the output must match; they join the evidence.
        Extra *fields* go to ``operation_for`` (``forbidden``, ``cells``, ...).
        """
        args = self._args(line)
        command = args[0]
        if requested is None:
            requested = self.requested(args)
        op = self.operation(args, read_only=read_only, paired=paired, **fields)
        allowed_exit = (
            frozenset({exit})
            if exit is not None
            else expected_exit(command, requested, frozen="FROZEN" in op.modifiers)
        )
        known = dict(known or {})
        exit_cell = known.pop("R", None)
        cells: dict[str, tuple[ExpectedCell, ...]] = {}
        for law, cell in known.items():
            label = f"{law} {cell.tracking}: {' '.join(args)}"
            cells[law] = (ExpectedCell(label, cell.match),)
            self.expected_all.add(label)
        converged = self.model.converged and requested == self.model
        model = StepModel(requested, self.kinds, converged, cells, self.expected_seen)
        before = self.observe()
        result = self._drive(args, output_cells=bool(set(cells) & _OUTPUT_LAWS))
        after = self._detect_user_keeps(self.observe(), result, model, op)
        self._check_exit(args, result, allowed_exit, exit_cell)
        check_cross_cutting(before, after, result, model, op)
        self._advance(op, args, requested, result)
        step = Step(" ".join(args), result, before, after)
        self.steps.append(step)
        if not (known or exit_cell):
            credit_step(step, credit, keep=keep, scope=self.scope, refusal=refusal, says=says)
        return step

    def pair(
        self,
        audit_line: str = "audit --ci",
        frozen_line: str = "install --frozen",
        *,
        audit_known: Mapping[str, Known] | None = None,
        frozen_known: Mapping[str, Known] | None = None,
    ) -> Step:
        """The I3 probe: ``audit --ci`` then ``install --frozen`` on the same state.

        ``audit --ci`` is expected clean exactly when the model predicts
        ``install --frozen`` succeeds.
        """
        frozen_exit = expected_exit("install", self.model, frozen=True)
        audit_exit = 0 if frozen_exit == frozenset({0}) else None
        audit = self.run(audit_line, read_only=True, exit=audit_exit, known=audit_known)
        return self.run(frozen_line, paired={"audit": audit.result.returncode}, known=frozen_known)

    def finish(self) -> None:
        """Every law-level strict known cell failed where it was declared."""
        missing = sorted(self.expected_all - self.expected_seen)
        assert not missing, f"strict expected failures that no longer fail: {missing}"

    # -- internals ---------------------------------------------------------------

    def _check_exit(
        self,
        args: tuple[str, ...],
        result: CommandResult,
        allowed: frozenset[int],
        cell: Known | None,
    ) -> None:
        """R: the exit code is the model's; a known ``R`` cell must keep mismatching."""
        message = (
            f"R: {' '.join(args)!r} exited {result.returncode}, expected {sorted(allowed)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
        if cell is None:
            assert result.returncode in allowed, message
            return
        label = f"R {cell.tracking}: {' '.join(args)}"
        assert result.returncode not in allowed, (
            f"strict expected failure {label} did not fail; remove its expectation"
        )
        assert re.search(cell.match, message), f"{label} failed differently: {message}"

    def _detect_user_keeps(
        self, after: Observation, result: CommandResult, model: StepModel, op: Operation
    ) -> Observation:
        """I4-X2: run ``audit --ci`` once when the step kept user bytes the gate refuses."""
        if not op.mutating or result.returncode != 0:
            return after
        if not user_kept_cells(after, model.allowed_surfaces()):
            return after
        audit = driver.run(self.world, ("audit", "--ci"), scope=self.scope)
        return replace(after, audit_ci=(audit.returncode, f"{audit.stdout}\n{audit.stderr}"))

    def _drive(self, args: tuple[str, ...], *, output_cells: bool) -> CommandResult:
        """Run *args*; a step with a known output-law cell bypasses the global hook.

        The global output hook (conftest) has no strict known cells, so such a
        step leaves I6, I8 and I9 to ``check_cross_cutting``, which checks the
        same laws and holds the cell strict.
        """
        if not output_cells:
            return driver.run(self.world, args, scope=self.scope)
        hooks = list(driver.OUTPUT_HOOKS)
        driver.OUTPUT_HOOKS.clear()
        try:
            return driver.run(self.world, args, scope=self.scope)
        finally:
            driver.OUTPUT_HOOKS.extend(hooks)

    def operation(self, args: tuple[str, ...], **fields: object) -> Operation:
        user = self.scope == "user"
        defaults: dict[str, object] = {
            "forbidden": {"project": ("*",)} if user else {"home": ("*",)},
            "allowed": {} if user else {"home": PROJECT_HOME_ALLOWED},
        }
        if args[0] not in _RECONCILING and args[0] not in {"uninstall", "prune"}:
            defaults["mutating"] = False
        merged = {**defaults, **{k: v for k, v in fields.items() if v is not None}}
        merged.setdefault("paired", {})
        if merged.get("paired") is None:
            merged["paired"] = {}
        return operation_for(args, scope=self.scope, **merged)

    def _advance(
        self, op: Operation, args: tuple[str, ...], requested: ModelState, result: CommandResult
    ) -> None:
        if result.returncode != 0 or "DRYRUN" in op.modifiers:
            return
        self.roots = self.planned_roots(args)
        if op.command in _RECONCILING and "FROZEN" not in op.modifiers:
            self.model = requested.installed()
        elif op.command in _RECONCILING:
            self.model = replace(requested, converged=self.model.converged)
        else:
            self.model = requested

    def requested(
        self,
        args: tuple[str, ...],
        *,
        model: ModelState | None = None,
        roots: Iterable[str] | None = None,
    ) -> ModelState:
        """The model after the user's request *args*, before the CLI reconciles it.

        From the scenario's current model and roots, or from *model* / *roots*.
        """
        model = self.model if model is None else model
        roots = frozenset(self.roots if roots is None else roots)
        names = self._names(args)
        command = args[0]
        if command not in _SELECTING or "--dry-run" in args:
            return model
        if command in {"install", "uninstall"} and (names or command == "uninstall"):
            return self._declared(self.planned_roots(args, roots), model)
        for name in names:
            if command == "approve":
                model = model.approve(name, self.kinds[name])
            elif command == "deny":
                model = model.deny(name, self.kinds[name])
        return model

    def planned_roots(
        self, args: tuple[str, ...], roots: Iterable[str] | None = None
    ) -> frozenset[str]:
        """The declared roots after a successful *args* (``install`` adds, ``uninstall`` drops)."""
        roots = frozenset(self.roots if roots is None else roots)
        if "--dry-run" in args:
            return roots
        if args[0] == "install":
            return roots | self._names(args)
        if args[0] == "uninstall":
            return roots - self._names(args)
        return roots

    def _names(self, args: tuple[str, ...]) -> frozenset[str]:
        by_spec = {spec: name for name, spec in self.specs.items()}
        return frozenset(by_spec[arg] for arg in args[1:] if arg in by_spec)

    def _declared(self, roots: Iterable[str], model: ModelState | None = None) -> ModelState:
        """*model* (default: the current one) with *roots* declared, plus their closure."""
        model = self.model if model is None else model
        closure = expected_lock_rows(ModelState(declared=frozenset(roots)), self.requires)
        return model.undeclare(*(model.declared - closure)).declare(*(closure - model.declared))

    def _args(self, line: str | Sequence[str]) -> tuple[str, ...]:
        args = driver.command_args(line)
        return tuple(self.specs.get(arg, arg) for arg in args)


def _entry_spec(entry: object) -> str:
    if isinstance(entry, Mapping):
        return str(entry.get("path") or entry.get("git") or "")
    return str(entry)


def serve_repository(world, slug: str, origin: Path) -> None:
    """Serve the bare repository *origin* as ``https://github.com/<slug>`` in *world*.

    For a repository a scenario authors itself (an annotated tag, a deeper
    graph); served for the rest of the worker session.
    """
    world.services.serve(slug, origin)


def identity(world, name: str) -> str:
    """The ``apm approve`` identity of a world package: its path, or ``owner/repo``."""
    ref = world.packages[name]
    return ref.spec.split("#", 1)[0] if ref.source == "git" else ref.spec


@dataclass
class Tree:
    """A small helper to write unmanaged files and check they survive byte-for-byte."""

    root: Path
    files: dict[str, bytes] = field(default_factory=dict)

    def write(self, relative: str, content: str) -> str:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("ascii")
        path.write_bytes(data)
        self.files[relative] = data
        return relative

    def assert_intact(self, label: str = "") -> None:
        damaged = sorted(
            rel
            for rel, data in self.files.items()
            if not (self.root / rel).is_file() or (self.root / rel).read_bytes() != data
        )
        assert not damaged, f"{label}: unmanaged files changed or removed: {damaged}"


def skill_package(factory, name: str, *, dependencies: Sequence[str] = ()) -> Path:
    """Author a world-shaped skill package *name* (its skill is named after it)."""
    package = factory.create(name, version="1.0.0", dependencies=list(dependencies))
    body = f"---\nname: {name}\ndescription: Scenario skill {name}.\n---\n\nUse {name}.\n"
    factory.add_skill(package, name, body)
    return package.root


# -- generated obligations ------------------------------------------------------

OBLIGATIONS_PATH = Path(__file__).parents[2] / "quality" / "lifecycle_obligations.json"
# Lanes whose generated obligations the scenario suites discharge (#4100-#4106).
SCENARIO_LANES = frozenset({"T-6a", "T-6b", "T-6c", "T-6d", "T-6e", "T-6g", "T-7b"})
NODE_ROOT = "tests/lifecycle/scenarios"


@dataclass(frozen=True)
class Credit:
    """A scenario node, the factor assignment one of its steps proves, and its strict cell.

    ``tests/lifecycle/generator/witnesses.py`` reads each module's ``CREDITS``:
    every scenario-lane obligation an assignment implies is ``required`` with
    *node* as witness, or ``known-failing`` with *tracking* when only strict
    known rows imply it.
    """

    node: str
    assignment: Mapping[str, str]
    tracking: str | None = None

    def __post_init__(self) -> None:
        assert self.node.startswith(f"{NODE_ROOT}/test_"), self.node
        assert self.tracking is None or re.fullmatch(r"#\d+", self.tracking), self.tracking


def credits(
    module_file: str,
    rows: Mapping[str, Iterable[Mapping[str, str]]],
    known: Mapping[str, str] | None = None,
) -> tuple[Credit, ...]:
    """``CREDITS`` of one module: test id (``name[param]``) -> the assignments it proves.

    *known* maps a test id to the issue of the strict known cell it carries.
    """
    module = f"{NODE_ROOT}/{Path(module_file).name}"
    known = known or {}
    return tuple(
        Credit(f"{module}::{test}", dict(assignment), known.get(test))
        for test, assignments in rows.items()
        for assignment in assignments
    )


@cache
def _scenario_obligations() -> frozenset[str]:
    data = json.loads(OBLIGATIONS_PATH.read_text(encoding="ascii"))["obligations"]
    return frozenset(key for key, entry in data.items() if entry["lane"] in SCENARIO_LANES)


def credited_ids(assignments: Iterable[Mapping[str, str]]) -> tuple[str, ...]:
    """Scenario-lane obligation ids every one of *assignments* implies."""
    lane = _scenario_obligations()
    return tuple(sorted({key for item in assignments for key in implied(item) if key in lane}))


@dataclass(frozen=True)
class ExpectedRefusal:
    """A pair whose designed outcome is a refusal that changes nothing.

    *cause* is a regex the command's output must match; *upholds* names the
    promise or law the refusal keeps and *source* cites where it is decided.
    """

    exit: int
    cause: str
    upholds: str
    source: str


# T-6b (#4101): ownership pairs whose designed outcome is a refusal. Declared,
# never inferred from what the CLI did: a refusal of any other pair fails the
# step, and so does a declared pair whose command succeeds.
# An org policy that denies a declared dependency refuses every resolving
# install and writes nothing (policy/install_preflight.py, dependency-denylist).
POLICY_BLOCK = ExpectedRefusal(
    1,
    r"Blocked by org policy .*Nothing was changed|Nothing was written",
    "P-INV-I4",
    "docs reference/cli/install.md: a blocked install exits 1 and changes nothing",
)
POLICY_REFUSALS = (
    "pair|DEPLOYED=clean|GATE=denied-dependency",
    "pair|DEPLOYED=edited|GATE=denied-dependency",
    "pair|DEPLOYED=present-provenance-unknown|GATE=denied-dependency",
    "pair|DEPLOYED=unowned|GATE=denied-dependency",
    "pair|GATE=denied-dependency|FORCE=on",
    "pair|GATE=denied-dependency|NET=offline",
    "pair|GATE=denied-dependency|NET=online",
    "pair|GATE=denied-dependency|SELECT=on",
    "pair|GATE=denied-dependency|UPDATE=on",
    *(
        f"pair|GATE=denied-dependency|TGT={shape}-{change}"
        for shape in ("single", "multi")
        for change in ("unchanged", "narrowed", "widened")
    ),
    "pair|SRC=git|GATE=denied-dependency",
    "pair|SRC=local-path|GATE=denied-dependency",
    "pair|SRC=marketplace-file|GATE=denied-dependency",
    "pair|SRC=marketplace-git|GATE=denied-dependency",
    "pair|SRC=marketplace-local|GATE=denied-dependency",
    "pair|SRC=native-plugin|GATE=denied-dependency",
    "pair|SRC=pkg-registry|GATE=denied-dependency",
    "pair|SRC=mcp-registry|GATE=denied-dependency",
    "pair|SRC=inline-mcp|GATE=denied-dependency",
)

EXPECTED_REFUSALS: Mapping[str, ExpectedRefusal] = {
    **dict.fromkeys(POLICY_REFUSALS, POLICY_BLOCK),
    "pair|DEPLOYED=edited|FROZEN=on": ExpectedRefusal(
        1,
        r"--frozen: deployed files do not match the hashes recorded in apm\.lock\.yaml",
        "P-INV-I3",
        "req-lk-017 (install/service.py _enforce_frozen_deployed_hashes)",
    ),
    "pair|DEPLOYED=present-provenance-unknown|FROZEN=on": ExpectedRefusal(
        1,
        r"--frozen requires apm\.lock\.yaml to exist",
        "P-LAW-lockfile.frozen_preserves_bytes",
        "req-lk-006: --frozen refuses a project with no lockfile (install/service.py enforce_frozen)",
    ),
    "pair|PRIM=mcp|DEPLOYED=edited": ExpectedRefusal(
        1,
        r"conflicts with the existing copilot connection .* Preserved that connection",
        "P-LAW-ownership.preserve_unowned",
        "integration/mcp_integrator_install.py: a native MCP conflict is never overwritten",
    ),
    "pair|SRC=inline-mcp|DEPLOYED=edited": ExpectedRefusal(
        1,
        r"conflicts with the existing copilot connection .* Preserved that connection",
        "P-LAW-ownership.preserve_unowned",
        "integration/mcp_integrator_install.py: a native MCP conflict is never overwritten",
    ),
    "pair|SRC=mcp-registry|DEPLOYED=edited": ExpectedRefusal(
        1,
        r"conflicts with the existing copilot connection .* Preserved that connection",
        "P-LAW-ownership.preserve_unowned",
        "integration/mcp_integrator_install.py: a native MCP conflict is never overwritten",
    ),
    "pair|OP=approve-deny|DEPLOYED=present-provenance-unknown": ExpectedRefusal(
        1,
        r"not found in installed packages",
        "P-LAW-ownership.preserve_unowned",
        "commands/approve.py: approve acts only on packages the lockfile records",
    ),
    "pair|PRIM=lsp|DEPLOYED=unowned": ExpectedRefusal(
        1,
        r"LSP server '\S+' conflicts with the existing",
        "P-LAW-ownership.preserve_unowned",
        "integration/lsp_integrator.py: a user's LSP entry of the same name is preserved (#4628)",
    ),
    "pair|PRIM=bin|DEPLOYED=unowned": ExpectedRefusal(
        1,
        r"already exists and the previous apm\.lock\.yaml does not record it",
        "P-LAW-ownership.preserve_unowned",
        "claude_plugins/registrar.py: a plugin root APM did not deploy is never replaced",
    ),
    "pair|OP=install|DEPLOYED=unowned": ExpectedRefusal(
        1,
        r"Installation refused: skill 'p-skill'",
        "P-LAW-ownership.preserve_unowned",
        "install/phases/integrate.py skill collision refusal (#4101)",
    ),
    "pair|OP=pack|DEPLOYED=edited": ExpectedRefusal(
        1,
        r"does not match the hash recorded in apm\.lock\.yaml",
        "P-LAW-ownership.preserve_unowned",
        "commands/pack.py: pack refuses a dependency whose installed files drifted",
    ),
    "pair|OP=uninstall|DEPLOYED=edited": ExpectedRefusal(
        1,
        r"Uninstall refused: deleting these tracked files needs deployment evidence",
        "P-LAW-ownership.preserve_unowned",
        "docs/src/content/docs/reference/cli/uninstall.md (edited files are kept)",
    ),
}


def _durable(changed: Iterable[str]) -> list[str]:
    return sorted(
        path
        for path in changed
        if not any(path == tree or path.startswith(tree + "/") for tree in _BOOKKEEPING_TREES)
    )


def credit_problems(
    ids: Iterable[str], *, exit: int, output: str, changed: Iterable[str], refusal: bool
) -> list[str]:
    """Why a step may not credit *ids*: a declared refusal must refuse, nothing else may."""
    ids = sorted(ids)
    if not refusal:
        return [
            f"{key}: declared refusal ({EXPECTED_REFUSALS[key].upholds}), but the step exited 0"
            for key in ids
            if key in EXPECTED_REFUSALS and exit == 0
        ]
    problems = [] if exit != 0 else ["a refusal credit needs a non-zero exit"]
    for key in ids:
        expected = EXPECTED_REFUSALS.get(key)
        if expected is None:
            problems.append(f"{key}: refusal is not a declared outcome (EXPECTED_REFUSALS)")
        elif exit != expected.exit:
            problems.append(f"{key}: exited {exit}, declared {expected.exit}")
        elif not re.search(expected.cause, output):
            problems.append(f"{key}: output does not name the cause {expected.cause!r}")
    durable = _durable(changed)
    if durable:
        problems.append(f"a refusal must change nothing, changed: {durable}")
    return problems


def credit_step(
    step: Step,
    assignments: Iterable[Mapping[str, str]],
    *,
    keep: Mapping[str, Iterable[str]] | None = None,
    scope: str = "project",
    refusal: bool = False,
    says: Iterable[str] = (),
) -> None:
    """Credit the obligations *assignments* imply through ``check_obligation``.

    A successful step credits success pairs; with *refusal*, a step credits
    only pairs declared in ``EXPECTED_REFUSALS``, and only when it exits with
    the declared code, names the cause and changes nothing durable. The
    evidence is the step's exact changed set (present or absent after it)
    plus the *keep* paths (present and byte-unchanged) and the *says*
    output patterns (an announced outcome).
    """
    ids = credited_ids(assignments)
    if not ids:
        return
    problems = credit_problems(
        ids,
        exit=step.result.returncode,
        output=step.output,
        changed=step.changed,
        refusal=refusal,
    )
    says = tuple(says)
    problems += [
        f"output does not say {pattern!r}"
        for pattern in says
        if not re.search(pattern, step.output)
    ]
    assert not problems, f"{step.line}:\n" + "\n".join(problems)
    if step.result.returncode != 0 and not refusal:
        return
    present: dict[str, set[str]] = {}
    absent: dict[str, set[str]] = {}
    after = step.after.artifacts
    for item in step.changed:
        root, path = item.split(":", 1)
        bucket = present if path in after.snapshot(root).paths else absent
        bucket.setdefault(root, set()).add(path)
    kept = {root: tuple(paths) for root, paths in (keep or {}).items()}
    for root, paths in kept.items():
        present.setdefault(root, set()).update(paths)
    exit_code = step.result.returncode
    causes = tuple(EXPECTED_REFUSALS[key].cause for key in ids) if refusal else ()
    causes += says
    evidence = Evidence(
        present=present,
        absent=absent,
        unchanged=kept,
        exit=exit_code,
        output_matches=causes,
        ascii_output=True,
    )
    strength = "e2e-evidence" if evidence.state_facts() else "e2e"
    for key in ids:
        check_obligation(
            key,
            strength=strength,
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=evidence,
            scope=scope,
        )
