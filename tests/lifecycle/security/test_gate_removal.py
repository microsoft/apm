"""Gate removal: a gate change never grows what is deployed (I4, #4107).

PROMISE: P-INV-I4 -- deny, revoke and tightening never widen what is
deployed; the next mutating command removes what is no longer allowed, and
says so (P-INV-I7). Source: tests/lifecycle/oracles.py (I4),
docs/src/content/docs/enterprise/control-executables.mdx.

Shared machinery for the G obligations ``G|<path>|<change>|<kind>``:

- ``gate_paths/<path>.py``: one ``PathSpec`` per G path (arrange + act
  steps, scope, announcement and per-change step overrides);
- ``gate_slices/<kind>.py``: one ``GateSlice`` per KIND (package, target and
  rows: exact id, surface files, strict expected failure);
- ``CHANGES`` here: how each gate change is applied by default (local deny,
  revoke, a definition change, org deny, org turning the gate on, policy
  denying the dependency).

Both directories are discovered; one parametrized test replays every row
through ``tests/lifecycle/rows.py`` (``check_cross_cutting`` I1-I9 after
every CLI step). ``GateReplay`` adds input edits, user scope, package key
variants and surfaces the core table does not observe, and checks as state
facts: the gated surface was deployed before the change; after it no CLI
step deploys a gated surface that was not already deployed; the final world
equals the reference model and the surface is gone. ``check_obligation``
then credits the row's G id with the first removing step's result, its
announcement and the row's snapshot facts. G ids without a row stay in the
ratchet baseline; ``bin`` has no world package that deploys it
(integration/skill_integrator.py _deploy_plugin_bin deploys bin/ only for
marketplace plugins to Claude at user scope).
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.model.expected import expected_deployed, expected_exit
from tests.lifecycle.model.state import GATED_KINDS
from tests.lifecycle.obligation_space import security_obligations
from tests.lifecycle.oracles import (
    Evidence,
    ExpectedCell,
    Observation,
    check_cross_cutting,
    check_obligation,
    operation_for,
)
from tests.lifecycle.rows import Row, RowReplay, _RowModel
from tests.lifecycle.security.gate_paths import Step, paths
from tests.lifecycle.security.gate_slices import GateRow, GateSlice, slices
from tests.lifecycle.snapshot import LifecycleSurfaceRoots
from tests.lifecycle.world import use_policy
from tests.lifecycle.world.services import DENIED_PACKAGE, ORG
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I4", "P-INV-I7"),
]

PROMISES = tuple(f"P-INV-I{law}" for law in range(1, 10))
STRENGTH = "e2e-evidence"

# (scope, kind, target) -> glob, or (glob, marker) for a shared file that must
# name the package's entry. World convention: "<pkg>-mcp", "<pkg>-server".
_SURFACES: Mapping[tuple[str, str, str], str | tuple[str, str]] = {
    ("project", "skill", "copilot"): ".agents/skills/{pkg}/SKILL.md",
    ("project", "hook", "copilot"): ".github/hooks/{pkg}-*.json",
    ("project", "mcp", "copilot"): (".mcp.json", '"{pkg}-mcp"'),
    ("project", "lsp", "copilot"): (".github/lsp.json", '"{pkg}-server"'),
    ("project", "skill", "claude"): ".claude/skills/{pkg}/SKILL.md",
    ("project", "native", "claude"): (".mcp.json", '"{pkg}-mcp"'),
    ("user", "skill", "copilot"): ".agents/skills/{pkg}/SKILL.md",
    ("user", "hook", "copilot"): ".copilot/hooks/{pkg}-*.json",
    ("user", "mcp", "copilot"): (".copilot/mcp-config.json", '"{pkg}-mcp"'),
    ("user", "lsp", "copilot"): (".copilot/lsp-config.json", '"{pkg}-server"'),
    ("user", "bin", "claude"): ".agents/plugins/{pkg}/bin/{pkg}",
}
# HOME prefixes a project command may write: receipts, bookkeeping, script
# trust and logs, the marketplace cache.
_PROJECT_HOME_ALLOWED = (
    ".apm/deployment-receipts",
    ".apm-local",
    ".local/state/gh",
    ".cache/apm",
    ".apm/scripts-trust.json",
    ".apm/scripts-trust.json.lock",
    ".apm/logs",
    ".apm/cache/marketplace",
)
# Commands after which nothing the model forbids may stay deployed (I4).
_GATE_REMOVING = frozenset({"install", "update", "uninstall", "prune"})
_RECONCILING = frozenset({"install", "update"})
_SCRIPT_LOG = "script-runs.log"
# policy-dep-deny: the world policy variant that denies the package of a kind,
# and the world packages each variant denies (world/services.py).
_DEP_DENY = {"lsp": "deny-lsp", "native": "deny-native"}
_DENIED: Mapping[str, frozenset[str]] = {
    "deny-pkg": frozenset({DENIED_PACKAGE}),
    "deny-lsp": frozenset({"p-lsp"}),
    "deny-native": frozenset({"p-native-claude", "p-native-codex", "p-native-claude-bin"}),
}


class GateReplay(RowReplay):
    """``RowReplay`` with input edits, user scope and the gate-removal checks."""

    def __init__(self, world, row: Row, *, scope: str = "project") -> None:
        super().__init__(world, replace(row, scope="project"))
        self.scope = scope
        self.ceiling: frozenset[str] | None = None
        self.witness: tuple[str, ...] = ()
        self.at_change: Observation | None = None
        # (before, result, after) of the first removing step after the change.
        self.removal: tuple[Observation, CommandResult, Observation] | None = None
        self.bundle_key = ""
        self.gate_off = False

    # -- steps -----------------------------------------------------------

    def step(self, step: object) -> None:
        if isinstance(step, Mapping) and "edit" in step:
            self._edit(step)
        else:
            super().step(step)

    def _edit(self, step: Mapping[str, object]) -> None:
        edit = str(step["edit"])
        key = str(step.get("pkg", ""))
        if "witness" in step:
            self._mark_change(tuple(step["witness"]))
        if edit == "revoke":
            self._edit_store(lambda block: _drop_entries(block, self._identities(key)))
            self.model = self.model.revoke(key, self.kinds[key])
        elif edit == "redefine":
            kind = str(step["kind"])
            ref = self.world.packages[key]
            if ref.source == "local-bundle":
                _redefine_bundle(Path(ref.spec), kind)
            else:
                _redefine(Path(ref.spec), kind)
            self.model = self.model.revoke(key, {kind})
        elif edit == "policy":
            variant = str(step["variant"])
            use_policy(self.world, variant)
            names = _DENIED.get(variant, frozenset())
            denied = [k for k in self.kinds if self.world.packages[k].name in names]
            self.model = self.model.with_policy(variant, denied)
            if variant == "exec-gate-on" and self.gate_off:
                # The org turns the gate on: nothing was ever approved, so every
                # executable the project deployed with the gate off now parks.
                for name, kinds in self.kinds.items():
                    self.model = self.model.revoke(name, kinds)
        elif edit == "gate-off":
            manifest = self._manifest_root() / "apm.yml"
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            data.pop("executables", None)
            manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
            self.gate_off = True
            for name, kinds in self.kinds.items():
                self.model = self.model.approve(name, kinds)
        elif edit in ("bundle-approve", "bundle-deny"):
            self._bundle_decision(key, "allow" if edit == "bundle-approve" else "deny")
        elif edit == "lifecycle":
            manifest = self.world.project / "apm.yml"
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            log = self.world.project / _SCRIPT_LOG
            command = f"echo run >> '{log}'"
            data["lifecycle"] = {
                "post-install": [{"type": "command", "command": command, "timeoutSec": 30}]
            }
            manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
        elif edit != "mark":
            raise ValueError(f"unknown edit {edit!r}")
        if "witness" in step:
            # The lock records each package's executable state, so a gate change
            # leaves it out of date until a resolving install reconciles it.
            self.model = self.model.with_lock("stale")

    def _mark_change(self, witness: tuple[str, ...]) -> None:
        self.witness = witness
        observation = self.observe()
        assert set(witness) <= observation.deployed, (
            f"{witness} must be deployed before the gate change; deployed "
            f"{sorted(observation.deployed)}"
        )
        self.at_change = observation
        self.ceiling = frozenset(s for s in observation.deployed if s.split("/")[1] in GATED_KINDS)

    def _bundle_decision(self, key: str, store: str) -> None:
        """Pin this exact bundle, as the install output tells the user to."""
        if not self.bundle_key:
            assert self.last is not None
            found = re.search(r'"(p-exec#[^"]+@sha256:[0-9a-f]+)"', self.last.stdout)
            assert found, f"bundle install printed no approval key:\n{self.last.stdout}"
            self.bundle_key = found.group(1)
        entry = {"hooks": True, "mcp": True, "bin": True}

        def decide(block: dict) -> None:
            for other in ("allow", "deny"):
                (block.get(other) or {}).pop(self.bundle_key, None)
            block.setdefault(store, {})[self.bundle_key] = entry

        self._edit_store(decide)
        if store == "allow":
            self.model = self.model.approve(key, self.kinds[key])
        else:
            self.model = self.model.deny(key, self.kinds[key])

    def _identities(self, key: str) -> tuple[str, ...]:
        ref = self.world.packages[key]
        return (ref.spec, ref.name, f"{ORG}/{ref.name}")

    def _edit_store(self, edit: Callable[[dict], None]) -> None:
        if self.scope == "project":
            manifest = self.world.project / "apm.yml"
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            edit(data.setdefault("executables", {}))
            manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
            return
        config = self.world.apm_home / "config.json"
        data = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        edit(data.setdefault("executables", {}))
        config.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # -- scope -----------------------------------------------------------

    def _manifest_root(self) -> Path:
        return self.world.project if self.scope == "project" else self.world.apm_home

    def _deploy_root(self) -> Path:
        return self.world.project if self.scope == "project" else self.world.home

    def _set_targets(self) -> None:
        manifest = self._manifest_root() / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["targets"] = list(self.row.targets)
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    # -- observation -----------------------------------------------------

    def observe(self) -> Observation:
        root = self._deploy_root()
        deployed: set[str] = set()
        managed: set[str] = set()
        for key, kinds in self.kinds.items():
            name = self.world.packages[key].name
            for kind in kinds:
                for target in self.model.targets:
                    surface = _SURFACES.get((self.scope, kind, target))
                    if surface is None:
                        continue
                    pattern, marker = surface if isinstance(surface, tuple) else (surface, "")
                    marker = marker.format(pkg=name)
                    hits = [
                        path
                        for path in root.glob(pattern.format(pkg=name))
                        if path.is_file() and marker in path.read_text(encoding="utf-8")
                    ]
                    if hits:
                        deployed.add(f"{key}/{kind}/{target}")
                        # A shared config file (MCP, LSP) is not owned by one package.
                        if not marker:
                            managed.update(path.relative_to(root).as_posix() for path in hits)
        cache_root = self.world.cache_root or self.world.root / "cache"
        workspace = self._manifest_root()
        user = self.scope == "user"
        state = LifecycleStateSnapshot.capture(
            workspace,
            surfaces=LifecycleSurfaceRoots(root, self.world.apm_home, cache_root, user),
        )
        return Observation(
            state=state,
            artifacts=ArtifactSnapshotSet.capture(
                {"project": self.world.project, "home": self.world.home}
            ),
            deployed=frozenset(deployed),
            deploy_root=root,
            lock=LockFile.read(workspace / "apm.lock.yaml"),
            managed_paths=frozenset(managed),
        )

    def check_then(self) -> None:
        observed = set(self.observe().deployed)
        wanted = {
            "/".join(cell)
            for cell in expected_deployed(self.model, self.kinds)
            if (self.scope, cell[1], cell[2]) in _SURFACES
        }
        assert observed == wanted, f"deployed {sorted(observed)}, model {sorted(wanted)}"
        gone = set(self.witness) & observed
        assert not gone, f"I4: {sorted(gone)} still deployed after the gate change"

    # -- one CLI step ----------------------------------------------------

    def _args(self, line: str) -> tuple[str, ...]:
        raw = driver.command_args(line)
        args = []
        for arg in raw:
            ref = self.world.packages[arg] if arg in self.kinds else None
            if arg == "MARKETPLACE_URL":
                args.append(self.world.services.marketplace_url)
            elif ref is None:
                args.append(arg)
            elif raw[0] == "approve" and ref.source == "marketplace":
                # approve names a marketplace package by owner/name; the spec form
                # is not found. Only approve maps this way.
                args.append(f"{ORG}/{ref.name}")
            else:
                args.append(ref.spec)
        return tuple(args)

    def _fields(self, args: tuple[str, ...], read_only: bool) -> dict[str, object]:
        if self.scope == "project" and args[0] != "marketplace":
            fields: dict[str, object] = {
                "forbidden": {"home": ("*",)},
                "allowed": {"home": _PROJECT_HOME_ALLOWED},
            }
        else:
            fields = {"forbidden": {"project": ("*",)}, "allowed": {}}
        fields["read_only"] = read_only or "--dry-run" in args
        if args[0] not in _GATE_REMOVING:
            fields["mutating"] = False
        return fields

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
        op = operation_for(
            args, scope=self.scope, paired=paired or {}, **self._fields(args, read_only)
        )
        frozen = "FROZEN" in op.modifiers
        allowed_exit = (
            frozenset({pinned_exit})
            if pinned_exit is not None
            else expected_exit(args[0], requested, frozen=frozen)
        )
        cells: dict[str, tuple[ExpectedCell, ...]] = {}
        for cell in self.row.known_cells:
            if cell["step"] == line:
                label = f"{cell['id']} (tracking {cell['tracking']})"
                cells[cell["law"]] = (
                    *cells.get(cell["law"], ()),
                    ExpectedCell(label, cell["match"]),
                )
        converged = self.model.converged and requested == self.model
        model = _RowModel(requested, self.kinds, converged, cells, self.expected_seen)
        before = self.observe()
        result = driver.run(self.world, args, scope=self.scope)
        after = self.observe()
        assert result.returncode in allowed_exit, (
            f"R: {line!r} exited {result.returncode}, model expects {sorted(allowed_exit)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
        check_cross_cutting(before, after, result, model, op)
        if self.ceiling is not None:
            grown = {s for s in after.deployed if s.split("/")[1] in GATED_KINDS} - self.ceiling
            assert not grown, f"I4: {line!r} deployed {sorted(grown)} after the gate change"
            if self.removal is None and args[0] in _GATE_REMOVING:
                self.removal = (before, result, after)
        self._advance_model(op, requested, result)
        self.last = result
        return result

    def _advance_model(self, op, requested, result: CommandResult) -> None:
        if result.returncode != 0 or "DRYRUN" in op.modifiers:
            return
        if op.command in _RECONCILING and "FROZEN" not in op.modifiers:
            self.model = requested.installed()
        elif op.command in _RECONCILING:
            self.model = replace(requested, converged=self.model.converged)
        else:
            self.model = requested


def _drop_entries(block: dict, identities: tuple[str, ...]) -> None:
    for store in ("allow", "deny"):
        entries = block.get(store) or {}
        for entry in list(entries):
            if any(entry == i or entry.startswith(i + "#") for i in identities):
                del entries[entry]


def _append_arg(node: object) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("args"), list):
            node["args"].append("--v2")
        for value in node.values():
            _append_arg(value)
    elif isinstance(node, list):
        for value in node:
            _append_arg(value)


def _redefine_bundle(bundle: Path, kind: str) -> None:
    """Repack the local bundle with a changed *kind* definition (a new bundle digest)."""
    with zipfile.ZipFile(bundle) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    member = {"hook": "hooks.json", "mcp": ".mcp.json"}[kind]
    (name,) = (n for n in entries if n.rsplit("/", 1)[-1] == member and n.count("/") == 1)
    old = hashlib.sha256(entries[name]).hexdigest()
    if kind == "hook":
        entries[name] = entries[name].replace(b"echo p-exec", b"echo p-exec-v2")
    else:
        data = json.loads(entries[name])
        _append_arg(data)
        entries[name] = (json.dumps(data, indent=2) + "\n").encode("ascii")
    new = hashlib.sha256(entries[name]).hexdigest()
    # The bundle's own lock pins every file's digest; keep it consistent.
    (lock,) = (n for n in entries if n.endswith("/apm.lock.yaml"))
    entries[lock] = entries[lock].replace(old.encode("ascii"), new.encode("ascii"))
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, data in entries.items():
            archive.writestr(entry, data)


def _redefine(root: Path, kind: str) -> None:
    """Change the definition of *kind* in the package at *root* (a new version of it)."""
    if kind == "hook":
        hook = root / ".apm" / "hooks" / "session.json"
        hook.write_text(hook.read_text("ascii").replace("echo p-exec", "echo p-exec-v2"), "ascii")
    elif kind == "native":
        config = root / ".mcp.json"
        data = json.loads(config.read_text("ascii"))
        _append_arg(data)
        config.write_text(json.dumps(data, indent=2) + "\n", "ascii")
    else:
        manifest = root / "apm.yml"
        data = yaml.safe_load(manifest.read_text("utf-8"))
        _append_arg(data.get("dependencies", {}).get(kind))
        manifest.write_text(yaml.safe_dump(data), "utf-8")


# -- change registry --------------------------------------------------------

_REMOVAL = r"(Cleaned|Removed) (\d+ )?stale|Parked|skipped|Filtered"


@dataclass(frozen=True)
class ChangeSpec:
    """How one gate change is applied, and what the removing step must say."""

    apply: Callable[[str, str], list[Step]]  # (package key, kind) -> steps
    announce: str
    gate_off_first: bool = False
    # The policy refuses the package itself: resolving steps exit 1 and keep
    # the world, and the user's way out is to uninstall it.
    refuses_package: bool = False


def _org(variant: str) -> Callable[[str, str], list[Step]]:
    return lambda key, kind: [{"edit": "policy", "variant": variant}]


# Shared defaults; a path module may replace one via ``PathSpec.change_steps``.
CHANGES: Mapping[str, ChangeSpec] = {
    "local-deny": ChangeSpec(lambda key, kind: [f"apm deny {key}"], rf"denied|{_REMOVAL}"),
    "revoke-approval": ChangeSpec(
        lambda key, kind: [{"edit": "revoke", "pkg": key}], rf"not approved|{_REMOVAL}"
    ),
    "definition-changed": ChangeSpec(
        lambda key, kind: [{"edit": "redefine", "pkg": key, "kind": kind}],
        r"definition changed since approval",
    ),
    "org-deny": ChangeSpec(_org("exec-deny-all"), rf"deny_all|denied|{_REMOVAL}"),
    "org-enables-gate": ChangeSpec(
        _org("exec-gate-on"), rf"not approved|{_REMOVAL}", gate_off_first=True
    ),
    "policy-dep-deny": ChangeSpec(
        lambda key, kind: [{"edit": "policy", "variant": _DEP_DENY.get(kind, "deny-pkg")}],
        r"denied by pattern",
        refuses_package=True,
    ),
}


# -- rows -------------------------------------------------------------------


PATHS = paths()


def registered() -> list[tuple[GateSlice, GateRow]]:
    return [(gate_slice, row) for gate_slice in slices() for row in gate_slice.rows]


def build_row(gate_slice: GateSlice, gate_row: GateRow) -> tuple[Row, str, str]:
    """The replay row of one G obligation, its scope and its announcement regex."""
    path, change, kind = gate_row.parts
    spec, how = PATHS[path], CHANGES[change]
    key = gate_slice.package + spec.key_suffix
    witness = [] if kind not in GATED_KINDS else [f"{key}/{kind}/{gate_slice.target}"]
    arrange = spec.arrange(key)
    given: list[Step] = []
    if how.gate_off_first:
        given.append({"edit": "gate-off"})
        # With the gate off nothing asks for an approval (and a bundle install
        # prints no approval key), so the arrange steps approve nothing.
        arrange = [
            step
            for step in arrange
            if not str(step).startswith("apm approve")
            and not (isinstance(step, Mapping) and step.get("edit") == "bundle-approve")
        ]
    given += arrange[:1]
    if spec.sibling:
        given.append("apm install p-skill")
    given += arrange[1:]
    apply = spec.change_steps.get(change, how.apply)
    when: list[Step] = [{"edit": "mark", "witness": witness}, *apply(key, kind)]
    when += [step.format(key=key) if isinstance(step, str) else step for step in spec.act]
    if how.refuses_package:
        when.append(f"apm uninstall {key}")
    announce = gate_row.announce or spec.announce or how.announce
    row = Row(
        id=gate_row.id,
        promises=PROMISES,
        packages=(key, "p-skill") if spec.sibling else (key,),
        targets=(gate_slice.target,),
        scope="project",
        given=tuple(given),
        when=tuple(when),
        cell={"OP": path, "PRIM": kind, "GATE": change, "SCOPE": spec.scope},
    )
    return row, spec.scope, announce


def _param(gate_slice: GateSlice, gate_row: GateRow):
    marks = []
    if gate_row.smoke:
        marks.append(pytest.mark.lifecycle_smoke)
    if gate_row.xfail:
        marks.append(pytest.mark.xfail(strict=True, raises=AssertionError, reason=gate_row.xfail))
    return pytest.param(gate_slice, gate_row, id=gate_row.id, marks=marks)


def test_every_registered_row_is_a_g_obligation() -> None:
    universe = security_obligations()["G"]
    ids = [row.id for _, row in registered()]
    assert len(ids) == len(set(ids)), "a G id is registered twice"
    assert set(ids) <= set(universe), sorted(set(ids) - set(universe))
    for gate_slice, row in registered():
        path, change, kind = row.parts
        assert kind == gate_slice.kind, row.id
        assert path in PATHS and change in CHANGES, row.id
        assert universe[row.id] == STRENGTH, row.id


@pytest.mark.parametrize(("gate_slice", "gate_row"), [_param(*item) for item in registered()])
def test_gate_change_removes_on_the_next_mutating_command(
    lifecycle_world, gate_slice: GateSlice, gate_row: GateRow
) -> None:
    row, scope, announce = build_row(gate_slice, gate_row)
    replay = GateReplay(lifecycle_world, row, scope=scope)
    replay.replay()
    assert replay.removal is not None, "no removing step ran after the gate change"
    before, result, after = replay.removal
    # A removing step that succeeds deletes the surface files (or edits shared
    # ones); one that refuses or only plans keeps them byte for byte.
    removed = result.returncode == 0 and "--dry-run" not in result.command
    unchanged = dict(gate_row.unchanged)
    absent: dict[str, tuple[str, ...]] = {}
    if removed and not gate_row.shared:
        absent = dict(gate_row.surface)
    elif not removed:
        unchanged = {**unchanged, **gate_row.surface}
    check_obligation(
        gate_row.id,
        strength=STRENGTH,
        before=before,
        after=after,
        result=result,
        evidence=Evidence(absent=absent, unchanged=unchanged, output_matches=(announce,)),
        scope=scope,
    )
