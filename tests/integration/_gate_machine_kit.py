"""M-GATE: the executable-trust sub-machine of the generated lifecycle model (#4031).

The machine drives the real CLI through declare and undeclare of executable
(hook), LSP and native-plugin content, approve, deny, revoke, policy
tightening, install, install --frozen, update, uninstall, prune and
audit --ci, at project scope and at user scope with
``APM_HOME`` outside ``HOME``. After every step ``check_cross_cutting`` runs
the I1-I9 laws plus the reference-model law ``R``.

Every rule emits ``cell:<OP>|<SRC>|<PRIM>|<GATE>|<SCOPE>`` as a Hypothesis
event and records it in ``VISITED_CELLS`` for the session hook.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import hypothesis
import yaml
from hypothesis import strategies as st
from hypothesis.control import currently_in_test_context
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule

from apm_cli.deps.lockfile import LockFile
from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_oracle import (
    ExpectedCell,
    Observation,
    Operation,
    check_cross_cutting,
    partition_expected,
    strict_misses,
)
from tests.utils.lifecycle_state import LifecycleStateSnapshot, LifecycleSurfaceRoots

SCOPES = ("project", "user")
KINDS = ("hook", "lsp", "plugin")
SURFACES = KINDS
VERDICTS = ("approve", "deny", "revoke")
POLICY_LEVELS = ("none", "org-deny", "org-deny-all")
_PACKAGES = {"hook": "hook-kit", "lsp": "lsp-kit", "plugin": "plugin-kit"}
_HOOK_FILE = {
    "project": ".github/hooks/hook-kit-session.json",
    "user": ".copilot/hooks/hook-kit-session.json",
}
_LSP_FILE = {"project": ".github/lsp.json", "user": ".copilot/lsp-config.json"}
_LSP_SERVER = "kit-lsp"
_PLUGIN_MARKERS = ("plugin-kit@apm", "plugin-kit-mcp")
_GIT_REMOTE = "https://github.com/acme/project.git"
_POLICY_REPO = "acme/.github-private"

VISITED_CELLS: set[str] = set()
"""Every ``cell:`` event emitted in this process; written by the session hook."""
COMPLETED_SPINES: set[str] = set()
"""Scopes whose mandatory GATE spine completed in this process."""


@dataclass(frozen=True)
class KnownCell:
    """A cell the machine shows failing on main: a STRICT expected failure.

    ``match`` is mandatory: only violations whose message matches are excused,
    so an unrelated regression on the same step still fails. A ``per_step``
    cell must fail wherever ``applies`` holds; otherwise the spine proves it
    still fails at least once.
    """

    cell_id: str
    law: str
    tracking: str
    applies: Callable[
        [GateMachine, Operation, Observation, Observation, CommandResult | None], bool
    ]
    match: str
    scopes: tuple[str, ...] = SCOPES
    per_step: bool = True

    @property
    def label(self) -> str:
        return f"{self.cell_id} (tracking {self.tracking})"


def _hook_parked_with_wrong_reason(
    machine: GateMachine, op: Operation, before, after, result
) -> bool:
    del before, after
    return (
        result is not None
        and op.name in {"install", "update"}
        and "hook" in machine.declared
        and machine.gate_state("hook") in {"deny", "org-deny", "org-deny-all"}
        and "hooks skipped (not approved" in " ".join(result.stdout.split())
    )


def _frozen_after_tightening(machine: GateMachine, op: Operation, before, after, result) -> bool:
    del machine, result
    return (
        op.name == "install-frozen"
        and "hook" in before.deployed
        and "hook" not in after.deployed
        and _HOOK_KEY in _recorded(after)
    )


_HOOK_KEY = "hook-file"


def _recorded(observation: Observation) -> frozenset[str]:
    """Which gate files the lock still records (as surface keys)."""
    lock = observation.lock
    if lock is None:
        return frozenset()
    recorded: set[str] = set()
    for dependency in lock.get_package_dependencies():
        recorded.update(dependency.deployed_file_hashes)
    recorded.update(lock.local_deployed_file_hashes)
    for record in lock.deployment_ledger.records.values():
        recorded.add(record.locator.value)
    keys = set()
    if any(path.endswith("hook-kit-session.json") for path in recorded):
        keys.add(_HOOK_KEY)
    return frozenset(keys)


KNOWN_CELLS: tuple[KnownCell, ...] = (
    KnownCell(
        "gate-cell-b",
        "R",
        "#4042",
        lambda machine, op, before, after, result: (
            op.name == "approve" and op.cell_kinds == ("plugin",) and machine.plugin_installed
        ),
        match=r"^approve plugin recorded no grant",
    ),
    KnownCell(
        "gate-cell-c-lock",
        "I1",
        "#4043",
        _frozen_after_tightening,
        match=r"^recorded file is missing: .*hook-kit-session\.json$",
    ),
    KnownCell(
        "gate-cell-c-pair",
        "I3",
        "#4043",
        lambda machine, op, before, after, result: (
            op.name == "install-frozen"
            and bool(op.paired)
            and _frozen_after_tightening(machine, op, before, after, result)
        ),
        match=r"^audit --ci exited 1 but install --frozen exited 0$",
        scopes=("project",),
    ),
    KnownCell(
        "gate-cell-d",
        "R",
        "#4044",
        _hook_parked_with_wrong_reason,
        match=r"^a denied hook is reported as merely unapproved$",
    ),
    KnownCell(
        "gate-cell-g",
        "I3",
        "#4050",
        lambda machine, op, before, after, result: (
            op.name == "install-frozen" and op.paired.get("audit") == 0 and before.lock is None
        ),
        match=r"^audit --ci exited 0 but install --frozen exited 1$",
    ),
    KnownCell(
        "gate-cell-g",
        "I2",
        "#4050",
        lambda machine, op, before, after, result: (
            op.name == "install-frozen" and machine.converged and before.lock is None
        ),
        match=r"^converged install-frozen exited 1$",
    ),
    KnownCell(
        "gate-cell-e",
        "I7",
        "#4045",
        lambda machine, op, before, after, result: (
            op.name == "uninstall" and op.cell_kinds == ("lsp",) and "lsp" in before.deployed
        ),
        match=r"^failed uninstall changed the (manifest|lockfile)$",
    ),
)


@dataclass
class GateWorld:
    """One isolated world: three local packages and one consumer."""

    scope: str
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    workspace: Path
    cwd: Path
    deploy_root: Path
    apm_home: Path
    packages: dict[str, Path]
    roots: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def create(cls, root: Path, engine_command: tuple[str, ...], scope: str) -> GateWorld:
        isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
        env = isolated.subprocess_env()
        env["APM_NON_INTERACTIVE"] = "1"
        # Wide output keeps one diagnostic per line, so I9 never sees wrap artifacts.
        env["COLUMNS"] = "400"
        runner = ApmLifecycleRunner(engine_command, timeout_seconds=120)
        sources = isolated.package_root
        packages = {
            "hook": _hook_package(sources / "hook-kit"),
            "lsp": _lsp_package(sources / "lsp-kit"),
            "plugin": write_agent_plugin(sources / "plugin-kit", name="plugin-kit", with_mcp=True),
        }
        if scope == "project":
            workspace = isolated.work_root / "gate"
            workspace.mkdir()
            cwd = deploy_root = workspace
            apm_home = isolated.config_root
            roots = {"project": workspace, "home": isolated.home}
        else:
            apm_home = root / "apm-home"
            apm_home.mkdir()
            env["APM_HOME"] = str(apm_home)
            (isolated.home / ".copilot").mkdir()
            workspace = apm_home
            cwd = isolated.work_root / "elsewhere"
            cwd.mkdir()
            deploy_root = isolated.home
            roots = {"cwd": cwd, "home": isolated.home, "apm-home": apm_home}
        manifest = {
            "name": "gate-consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": []},
            # The executables gate is on and nothing is approved yet.
            "executables": {"allow": {}},
        }
        (workspace / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        return cls(
            scope,
            isolated,
            env,
            runner,
            workspace,
            cwd,
            deploy_root,
            apm_home,
            packages,
            roots,
        )

    def run(self, args: tuple[str, ...], label: str) -> CommandResult:
        return self.runner.run(args, cwd=self.cwd, env=self.env, scenario_id=f"gate-{label}")

    def global_flag(self) -> tuple[str, ...]:
        return ("--global",) if self.scope == "user" else ()

    def manifest(self) -> dict:
        return yaml.safe_load((self.workspace / "apm.yml").read_text(encoding="utf-8"))

    def write_manifest(self, data: dict) -> None:
        (self.workspace / "apm.yml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def lock(self) -> LockFile | None:
        return LockFile.read(self.workspace / "apm.lock.yaml")

    def store(self) -> dict:
        """The approval store the CLI writes at this scope (manifest or user config)."""
        if self.scope == "project":
            return self.manifest().get("executables") or {}
        config = self.apm_home / "config.json"
        if not config.is_file():
            return {}
        return json.loads(config.read_text(encoding="utf-8")).get("executables") or {}

    def edit_store(self, edit: Callable[[dict], None], *, manifest: bool = False) -> None:
        if manifest or self.scope == "project":
            data = self.manifest()
            block = data.setdefault("executables", {})
            edit(block)
            self.write_manifest(data)
            return
        config = self.apm_home / "config.json"
        data = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        block = data.setdefault("executables", {})
        edit(block)
        config.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def seed_policy(self, level: str) -> None:
        """Tighten the org policy: seed a fresh cached org policy, keeping discovery offline."""
        if not (self.deploy_root / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=self.deploy_root, env=self.env, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", _GIT_REMOTE],
                cwd=self.deploy_root,
                env=self.env,
                check=True,
            )
        executables = (
            {"deny_all": True}
            if level == "org-deny-all"
            else {"deny": [str(self.packages["hook"])]}
        )
        policy = {
            "name": "org",
            "version": "1.0",
            "enforcement": "block",
            "executables": executables,
        }
        source = self.isolated.root / "policy.yml"
        source.write_text(yaml.safe_dump(policy), encoding="utf-8")
        script = (
            "import sys\nfrom pathlib import Path\n"
            "from apm_cli.policy.discovery import _write_cache\n"
            "from apm_cli.policy.parser import load_policy\n"
            "policy, _ = load_policy(Path(sys.argv[2]).read_text())\n"
            f"_write_cache({_POLICY_REPO!r}, policy, Path(sys.argv[1]))\n"
        )
        subprocess.run(
            [sys.executable, "-c", script, str(self.deploy_root), str(source)],
            env=self.env,
            check=True,
        )

    def observe(self) -> Observation:
        deployed: set[str] = set()
        if (self.deploy_root / _HOOK_FILE[self.scope]).is_file():
            deployed.add("hook")
        lsp = self.deploy_root / _LSP_FILE[self.scope]
        if lsp.is_file() and _LSP_SERVER in json.loads(lsp.read_text("utf-8")).get(
            "lspServers", {}
        ):
            deployed.add("lsp")
        state = LifecycleStateSnapshot.capture(
            self.workspace,
            surfaces=LifecycleSurfaceRoots(
                self.deploy_root, self.apm_home, self.isolated.cache_root, self.scope == "user"
            ),
        )
        surfaces = (*state.native_registries, *state.mcp_config_files)
        if any(
            content is not None and any(marker.encode() in content for marker in _PLUGIN_MARKERS)
            for _label, content in surfaces
        ):
            deployed.add("plugin")
        return Observation(
            state=state,
            artifacts=ArtifactSnapshotSet.capture(self.roots),
            deployed=frozenset(deployed),
            deploy_root=self.deploy_root,
            lock=self.lock(),
            managed_paths=frozenset({_HOOK_FILE[self.scope]}),
        )


def _hook_package(root: Path) -> Path:
    (root / ".apm" / "hooks").mkdir(parents=True)
    (root / "apm.yml").write_text("name: hook-kit\nversion: 1.0.0\n", encoding="utf-8")
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo HOOK"}]}]}}
    (root / ".apm" / "hooks" / "session.json").write_text(json.dumps(hooks), encoding="utf-8")
    return root


def _lsp_package(root: Path) -> Path:
    root.mkdir(parents=True)
    server = {
        "name": _LSP_SERVER,
        "command": "inert-language-server",
        "args": ["--stdio"],
        "extensionToLanguage": {".fixture": "fixture"},
    }
    manifest = {"name": "lsp-kit", "version": "1.0.0", "dependencies": {"lsp": [server]}}
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return root


class GateMachine(RuleBasedStateMachine):
    """Reference model of the executable gate, checked against the real CLI."""

    def __init__(self, world: GateWorld) -> None:
        super().__init__()
        self.world = world
        self.scope = world.scope
        self.declared: set[str] = set()
        self.store_state: dict[str, str] = {}
        self.policy = "none"
        self.converged = False
        self.visited: set[str] = set()
        self.expected_seen: set[str] = set()

    # -- model ---------------------------------------------------------------

    @property
    def plugin_installed(self) -> bool:
        return self._locked("plugin")

    def _locked(self, kind: str) -> bool:
        lock = self.world.lock()
        path = str(self.world.packages[kind])
        return lock is not None and any(
            dependency.local_path == path for dependency in lock.get_package_dependencies()
        )

    def gate_state(self, surface: str) -> str:
        if self.policy == "org-deny-all":
            return "org-deny-all"
        if self.policy == "org-deny" and surface == "hook":
            return "org-deny"
        decision = self.store_state.get(surface)
        return {"allow": "allow", "deny": "deny"}.get(decision or "", "pending")

    def allowed(self, surface: str) -> bool:
        return self.gate_state(surface) == "allow"

    def allowed_surfaces(self) -> frozenset[str]:
        return frozenset(surface for surface in SURFACES if self.allowed(surface))

    def converged_before(self) -> bool:
        return self.converged

    def expected_cells(self, op, before, after, result) -> Mapping[str, tuple[ExpectedCell, ...]]:
        expected: dict[str, tuple[ExpectedCell, ...]] = {}
        for cell in KNOWN_CELLS:
            if cell.applies(self, op, before, after, result):
                expected[cell.law] = (
                    *expected.get(cell.law, ()),
                    ExpectedCell(cell.label, cell.match, cell.per_step),
                )
        return expected

    def record_expected(self, cell_id: str) -> None:
        self.expected_seen.add(cell_id)

    # -- steps ---------------------------------------------------------------

    def _cells(self, op: str, kinds: tuple[str, ...]) -> tuple[str, ...]:
        present = kinds or tuple(kind for kind in SURFACES if kind in self.declared)
        cells = []
        for kind in present or ("none",):
            source = {"none": "none", "all": "policy"}.get(kind, "path")
            gate = {"none": "none", "all": self.policy}.get(kind) or self.gate_state(kind)
            cell = f"cell:{op}|{source}|{kind}|{gate}|{self.scope}"
            cells.append(cell)
            VISITED_CELLS.add(cell)
            self.visited.add(cell)
            if currently_in_test_context():
                hypothesis.event(cell)
        return tuple(cells)

    def _forbidden(self) -> dict[str, tuple[str, ...]]:
        if self.scope == "project":
            # A project step may only write its receipts and bookkeeping under HOME.
            return {"home": ("*",)}
        return {"cwd": ("*",), "home": (".apm",)}

    def _allowed(self) -> dict[str, tuple[str, ...]]:
        if self.scope == "project":
            return {
                "home": (
                    ".apm/deployment-receipts",
                    ".apm-local",
                    ".local/state/gh",
                    ".cache/apm",
                )
            }
        return {}

    def _step(
        self,
        op: Operation,
        action: Callable[[], CommandResult | None],
        *,
        reference: Callable[[Observation, Observation, CommandResult | None], list[str]]
        | None = None,
    ) -> CommandResult | None:
        visited = self._cells(op.name, op.cell_kinds)
        before = self.world.observe()
        result = action()
        after = self.world.observe()
        problems = reference(before, after, result) if reference else []
        cells = self.expected_cells(op, before, after, result).get("R", ())
        unexpected, owners = partition_expected(problems, cells)
        misses = strict_misses(cells, owners)
        if unexpected or misses:
            raise AssertionError(
                f"R: after {op.name}: "
                + "; ".join(
                    [*unexpected, *(f"strict expected failure {m} did not fail" for m in misses)]
                )
            )
        for cell_id in owners:
            self.record_expected(cell_id)
        guarded = Operation(
            **{
                **op.__dict__,
                "forbidden": self._forbidden(),
                "allowed": self._allowed(),
                "cells": visited,
                "scope": self.scope,
            }
        )
        check_cross_cutting(before, after, result, _ExpectedWithoutReference(self), guarded)
        return result

    def _edit(self, name: str, kinds: tuple[str, ...], edit: Callable[[], None], **flags) -> None:
        self.converged = False
        self._step(Operation(name, kinds, edit_only=True, **flags), lambda: edit() or None)

    def _install_reference(self, mode: str):
        def reference(before, after, result) -> list[str]:
            if result is None or result.returncode != 0 or mode != "plain":
                return []
            problems = []
            for kind in ("hook", "lsp"):
                wanted = kind in self.declared and self.allowed(kind)
                if (kind in after.deployed) != wanted:
                    problems.append(f"{kind} deployed={kind in after.deployed}, expected {wanted}")
            if "plugin" in after.deployed and not self.allowed("plugin"):
                problems.append("plugin registered without approval")
            if _hook_parked_with_wrong_reason(self, Operation("install"), before, after, result):
                problems.append("a denied hook is reported as merely unapproved")
            return problems

        return reference

    # -- rules (at most ten) ---------------------------------------------------

    @rule(kind=st.sampled_from(KINDS))
    def declare(self, kind: str) -> None:
        if kind in self.declared:
            return self.undeclare(kind)

        def edit() -> None:
            data = self.world.manifest()
            data.setdefault("dependencies", {}).setdefault("apm", []).append(
                str(self.world.packages[kind])
            )
            self.world.write_manifest(data)

        self._edit("declare", (kind,), edit)
        self.declared.add(kind)
        return None

    def undeclare(self, kind: str) -> None:
        def edit() -> None:
            data = self.world.manifest()
            entries = data.get("dependencies", {}).get("apm", [])
            data["dependencies"]["apm"] = [
                e for e in entries if e != str(self.world.packages[kind])
            ]
            self.world.write_manifest(data)

        self._edit("undeclare", (kind,), edit)
        self.declared.discard(kind)

    @rule(surface=st.sampled_from(SURFACES), verdict=st.sampled_from(VERDICTS))
    def decide(self, surface: str, verdict: str) -> None:
        self.converged = False
        path = str(self.world.packages[surface])
        if verdict == "revoke":

            def revoke(block: dict) -> None:
                for store in ("allow", "deny"):
                    entries = block.get(store) or {}
                    for key in [k for k in entries if k == path or k.startswith(path + "#")]:
                        del entries[key]

            self._edit("revoke", (surface,), lambda: self.world.edit_store(revoke), tightening=True)
            self.store_state.pop(surface, None)
            return None
        user = ("--user",) if self.scope == "user" else ()
        locked = self._locked(surface)

        def reference(before, after, result) -> list[str]:
            if result is None or result.returncode != 0:
                return [f"{verdict} exited {getattr(result, 'returncode', None)}"]
            if verdict == "approve" and locked and not _store_grants(self.world.store(), path):
                return [f"approve {surface} recorded no grant for an installed package"]
            return []

        self._step(
            Operation(verdict, (surface,), tightening=verdict == "deny"),
            lambda: self.world.run((verdict, *user, path), f"{verdict}-{surface}"),
            reference=reference,
        )
        if verdict == "deny":
            self.store_state[surface] = "deny"
        elif _store_grants(self.world.store(), path) and not _store_denies(
            self.world.store(), path
        ):
            self.store_state[surface] = "allow"
        return None

    @rule(level=st.sampled_from(POLICY_LEVELS[1:]))
    @precondition(lambda self: self.policy != "org-deny-all")
    def set_policy(self, level: str) -> None:
        if POLICY_LEVELS.index(level) <= POLICY_LEVELS.index(self.policy):
            level = "org-deny-all"
        self._edit(
            f"set-policy-{level}", ("all",), lambda: self.world.seed_policy(level), tightening=True
        )
        self.policy = level

    @rule(mode=st.sampled_from(("plain", "frozen")))
    def install(self, mode: str) -> None:
        self._install(mode)

    def _install(self, mode: str, paired: Mapping[str, int] | None = None) -> CommandResult | None:
        args = ("install", *self.world.global_flag(), *(("--frozen",) if mode == "frozen" else ()))
        name = "install" if mode == "plain" else "install-frozen"
        result = self._step(
            Operation(
                name,
                mutating=True,
                paired=paired or {},
                modifiers=("FROZEN",) if mode == "frozen" else (),
            ),
            lambda: self.world.run(args, name),
            reference=self._install_reference(mode),
        )
        if mode == "plain" and result is not None and result.returncode == 0:
            self.converged = True
        return result

    @rule()
    def update(self) -> None:
        args = ("update", *self.world.global_flag(), "--yes")
        result = self._step(
            Operation("update", mutating=True),
            lambda: self.world.run(args, "update"),
            reference=self._install_reference("plain"),
        )
        self.converged = result is not None and result.returncode == 0 and self.converged

    @rule(kind=st.sampled_from(KINDS))
    def uninstall(self, kind: str) -> None:
        if kind not in self.declared:
            return
        args = ("uninstall", *self.world.global_flag(), str(self.world.packages[kind]))
        self.converged = False
        result = self._step(
            Operation("uninstall", (kind,), mutating=True),
            lambda: self.world.run(args, f"uninstall-{kind}"),
        )
        declared = self.world.manifest().get("dependencies", {}).get("apm", [])
        if result is not None and str(self.world.packages[kind]) not in declared:
            self.declared.discard(kind)

    @rule()
    @precondition(lambda self: self.scope == "project")
    def prune(self) -> None:
        self._step(Operation("prune", mutating=True), lambda: self.world.run(("prune",), "prune"))

    @rule(json_output=st.booleans())
    @precondition(lambda self: self.scope == "project")
    def audit_ci(self, json_output: bool) -> None:
        args = ("audit", "--ci", *(("--format", "json") if json_output else ()))
        audit = self._step(
            Operation("audit-ci", read_only=True, json_output=json_output),
            lambda: self.world.run(args, "audit-ci"),
        )
        assert audit is not None
        self._install("frozen", paired={"audit": audit.returncode})


class _ExpectedWithoutReference:
    """Adapter that hides the reference law from the cross-cutting oracle."""

    def __init__(self, machine: GateMachine) -> None:
        self._machine = machine

    def allowed_surfaces(self) -> frozenset[str]:
        return self._machine.allowed_surfaces()

    def converged_before(self) -> bool:
        return self._machine.converged_before()

    def expected_cells(self, op, before, after, result) -> Mapping[str, tuple[ExpectedCell, ...]]:
        cells = dict(self._machine.expected_cells(op, before, after, result))
        cells.pop("R", None)
        return cells

    def record_expected(self, cell_id: str) -> None:
        self._machine.record_expected(cell_id)


def _store_grants(store: dict, path: str) -> bool:
    return any(k == path or k.startswith(path + "#") for k in (store.get("allow") or {}))


def _store_denies(store: dict, path: str) -> bool:
    return any(k == path or k.startswith(path + "#") for k in (store.get("deny") or {}))
