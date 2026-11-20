"""Gate removal: a gate change never grows what is deployed (I4, #4107).

PROMISE: P-INV-I4 -- deny, revoke and tightening never widen what is
deployed; the next mutating command removes what is no longer allowed, and
says so (P-INV-I7). Source: tests/lifecycle/oracles.py (I4),
docs/src/content/docs/enterprise/control-executables.mdx.

Each cell is one (KIND, PATH, CHANGE) tuple, replayed through
``tests/lifecycle/rows.py`` so ``check_cross_cutting`` (I1-I9) runs after
every CLI step. ``GateReplay`` adds what the row schema cannot express yet:
input edits (revoke, definition change, org policy, gate off), user scope,
bundle and marketplace package keys, and surfaces the core table does not
observe (native plugin MCP, user-scope Copilot files). For every cell it also
checks, as state facts:

- the gated surface was deployed before the change (the removal is real);
- after the change no CLI step deploys a gated surface that was not
  deployed at the change (the deployed gated set never grows);
- the final world equals the reference model and the surface is gone;
- the removing step announces it (I7).

Every parameter row is named by its exact obligation id
``G|<path>|<change>|<kind>`` (security obligation export, lane T-7a); rows
with no G id (canvas: the export has no canvas kind) use ``X|...``. Coverage
is pairwise over KIND x PATH x CHANGE. Ids with no row, and why:

| ids | why |
|---|---|
| G|*|*|bin | bin/ deploys only for marketplace plugins to Claude at user scope |
|           | (integration/skill_integrator.py _deploy_plugin_bin); no world    |
|           | package ships a bin/ the gate could deploy                         |
| G|unpack|* | deprecated command; reads only --format apm bundles, none in the world |
| G|mcp-install|* | root-declared MCP servers are the user's own, not a dependency gate |
| G|*-g|org-* , G|native-plugin|org-* | org policy governs project installs only |
| lsp/native in bundle-install, marketplace-install | the world packs and lists p-exec only |
| definition-changed in bundle/marketplace | a bundle or a tag cannot change in place |
| native approve-based rows | approve cannot find a parked native plugin (#4154); |
|                           | they are strict expected failures naming it          |

Lifecycle scripts (the execute path APM runs itself) have no deployed file;
their rows count script runs instead (``test_lifecycle_script_gate``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.model.expected import expected_deployed, expected_exit
from tests.lifecycle.model.state import GATED_KINDS
from tests.lifecycle.oracles import ExpectedCell, Observation, check_cross_cutting, operation_for
from tests.lifecycle.rows import Row, RowReplay, _RowModel
from tests.lifecycle.snapshot import LifecycleSurfaceRoots
from tests.lifecycle.world import use_policy
from tests.lifecycle.world.services import DENIED_PACKAGE
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I4", "P-INV-I7"),
]

PROMISES = [f"P-INV-I{law}" for law in range(1, 10)]

# (scope, kind, target) -> glob, or (glob, marker) for a shared file that must
# name the package's entry. World convention: "<pkg>-mcp", "<pkg>-server".
_SURFACES: Mapping[tuple[str, str, str], str | tuple[str, str]] = {
    ("project", "skill", "copilot"): ".agents/skills/{pkg}/SKILL.md",
    ("project", "hook", "copilot"): ".github/hooks/{pkg}-*.json",
    ("project", "mcp", "copilot"): (".mcp.json", '"{pkg}-mcp"'),
    ("project", "lsp", "copilot"): (".github/lsp.json", '"{pkg}-server"'),
    ("project", "canvas", "copilot"): ".github/extensions/{pkg}/extension.mjs",
    ("project", "native", "claude"): (".mcp.json", '"{pkg}-mcp"'),
    ("user", "native", "claude"): (".claude/settings.json", '"{pkg}'),
    ("user", "skill", "copilot"): ".agents/skills/{pkg}/SKILL.md",
    ("user", "hook", "copilot"): ".copilot/hooks/{pkg}-*.json",
    ("user", "mcp", "copilot"): (".copilot/mcp-config.json", '"{pkg}-mcp"'),
    ("user", "lsp", "copilot"): (".copilot/lsp-config.json", '"{pkg}-server"'),
}
# HOME prefixes a project command may write: receipts, bookkeeping, script trust.
_PROJECT_HOME_ALLOWED = (
    ".apm/deployment-receipts",
    ".apm-local",
    ".local/state/gh",
    ".cache/apm",
    ".apm/scripts-trust.json",
    ".apm/scripts-trust.json.lock",
    ".apm/logs",
)
# Commands after which nothing the model forbids may stay deployed (I4).
_GATE_REMOVING = frozenset({"install", "update", "uninstall", "prune"})
_RECONCILING = frozenset({"install", "update"})


class GateReplay(RowReplay):
    """``RowReplay`` with input edits, user scope and the gate-removal checks."""

    def __init__(self, world, row: Row, *, scope: str = "project") -> None:
        super().__init__(world, replace(row, scope="project"))
        self.scope = scope
        self.ceiling: frozenset[str] | None = None
        self.witness: tuple[str, ...] = ()

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
            self.witness = tuple(step["witness"])
            deployed = self.observe().deployed
            assert set(self.witness) <= deployed, (
                f"{self.witness} must be deployed before the gate change; deployed "
                f"{sorted(deployed)}"
            )
            self.ceiling = frozenset(s for s in deployed if s.split("/")[1] in GATED_KINDS)
        if edit == "revoke":
            self._edit_store(lambda block: _drop_entries(block, self._identities(key)))
            self.model = self.model.revoke(key, self.kinds[key])
        elif edit == "redefine":
            kind = str(step["kind"])
            _redefine(Path(self.world.packages[key].spec), kind)
            self.model = self.model.revoke(key, {kind})
        elif edit == "policy":
            variant = str(step["variant"])
            use_policy(self.world, variant)
            denied = [k for k in self.kinds if self.world.packages[k].name == DENIED_PACKAGE]
            self.model = self.model.with_policy(variant, denied if variant == "deny-pkg" else ())
        elif edit == "gate-off":
            manifest = self._manifest_root() / "apm.yml"
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            data.pop("executables", None)
            manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
            for name, kinds in self.kinds.items():
                self.model = self.model.approve(name, kinds)
        elif edit == "lifecycle":
            manifest = self.world.project / "apm.yml"
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            data["lifecycle"] = _lifecycle_block(self.world, str(step.get("tag", "v1")))
            manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
        elif edit != "mark":
            raise ValueError(f"unknown edit {edit!r}")

    def _identities(self, key: str) -> tuple[str, ...]:
        ref = self.world.packages[key]
        return (ref.spec, ref.name)

    def _edit_store(self, edit) -> None:
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
        then = self.row.then
        if "exit" in then:
            assert self.last is not None and self.last.returncode == then["exit"], self.last
        observed = set(self.observe().deployed)
        if then.get("deployed") == "expected":
            wanted = {
                "/".join(cell)
                for cell in expected_deployed(self.model, self.kinds)
                if (self.scope, cell[1], cell[2]) in _SURFACES
            }
            assert observed == wanted, f"deployed {sorted(observed)}, model {sorted(wanted)}"
        gone = set(self.witness) & observed
        assert not gone, f"I4: {sorted(gone)} still deployed after the gate change"
        if "announce" in then:
            output = " ".join(f"{self.announced}".split())
            assert re.search(str(then["announce"]), output), f"I7: not announced: {output[-400:]}"

    # -- one CLI step ----------------------------------------------------

    def _args(self, line: str) -> tuple[str, ...]:
        url = self.world.services.marketplace_url
        return tuple(url if arg == "MARKETPLACE_URL" else arg for arg in super()._args(line))

    def _fields(self, args: tuple[str, ...], read_only: bool) -> dict[str, object]:
        if self.scope == "project":
            fields: dict[str, object] = {
                "forbidden": {"home": ("*",)},
                "allowed": {"home": _PROJECT_HOME_ALLOWED},
            }
        else:
            fields = {"forbidden": {"project": ("*",)}, "allowed": {}}
        if args[0] == "marketplace":
            fields["forbidden"] = {"project": ("*",)}
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
        fields = self._fields(args, read_only)
        op = operation_for(args, scope=self.scope, paired=paired or {}, **fields)
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
        if self.ceiling is not None and op.mutating:
            self.announced += f"{result.stdout}\n{result.stderr}\n"
        if result.returncode == 0 and op.command in _RECONCILING and not frozen:
            self.model = requested.installed()
        elif result.returncode == 0 and op.command in _RECONCILING:
            self.model = replace(requested, converged=self.model.converged)
        elif result.returncode == 0:
            self.model = requested
        self.last = result
        return result

    announced = ""


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


def _redefine(root: Path, kind: str) -> None:
    """Change the definition of *kind* in the package at *root* (a new version of it)."""
    if kind == "hook":
        hook = root / ".apm" / "hooks" / "session.json"
        hook.write_text(hook.read_text("ascii").replace("echo p-exec", "echo p-exec-v2"), "ascii")
    elif kind == "canvas":
        (script,) = (p for p in root.rglob("extension.mjs") if "apm_modules" not in p.parts)
        script.write_text(script.read_text("ascii") + "// v2\n", "ascii")
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


def _script_log(world) -> Path:
    return world.root / "script-runs.log"


def _lifecycle_block(world, tag: str) -> dict[str, object]:
    command = f"echo {tag} >> '{_script_log(world)}'"
    return {"post-install": [{"type": "command", "command": command, "timeoutSec": 30}]}


# -- the cell table ---------------------------------------------------------

# KIND -> (package key, target); p-exec carries hook, mcp and bin.
_KINDS: Mapping[str, tuple[str, str]] = {
    "hook": ("p-exec", "copilot"),
    "mcp": ("p-exec", "copilot"),
    "lsp": ("p-lsp", "copilot"),
    "canvas": ("p-canvas", "copilot"),
    "native": ("p-native-claude", "claude"),
}
# PATH -> (package key suffix, scope, the steps that must remove the surface).
_PATHS: Mapping[str, tuple[str, str, tuple[object, ...]]] = {
    "install": ("", "project", ("apm install",)),
    "frozen": ("", "project", ("apm install --frozen",)),
    "update": ("", "project", ("apm update --yes",)),
    "uninstall-prune-rebuild": ("", "project", ("apm uninstall p-skill", "apm prune")),
    "audit-ci": ("", "project", ({"pair": ["apm audit --ci", "apm install --frozen"]},)),
    "dry-run": ("", "project", ("apm install --dry-run", "apm install")),
    "install-g": ("", "user", ("apm install",)),
    "frozen-g": ("", "user", ("apm install --frozen",)),
    "bundle-install": (":bundle", "project", ("apm install",)),
    "marketplace-install": (":marketplace", "project", ("apm install",)),
    "native-plugin": ("", "user", ("apm install",)),
}
# CHANGE -> what the removing step must say (I7), as a regex.
_REMOVAL = r"(Cleaned|Removed) (\d+ )?stale|Parked|skipped|Filtered"
_ANNOUNCE = {
    "local-deny": rf"denied|{_REMOVAL}",
    "revoke-approval": rf"not approved|{_REMOVAL}",
    "definition-changed": r"definition changed since approval",
    "org-deny": rf"deny_all|denied|{_REMOVAL}",
    "org-enables-gate": rf"deny_all|denied|{_REMOVAL}",
    "policy-dep-deny": r"denied by pattern",
}
# The one step left for a user whose dependency the policy now denies: every
# resolving step refuses it (exit 1, state kept), so they uninstall it.
_DENY_PKG_TAIL = "apm uninstall {key}"


def _change(change: str, key: str, kind: str, witness: list[str]) -> list[object]:
    if change == "local-deny":
        return [{"edit": "mark", "witness": witness}, f"apm deny {key}"]
    if change == "revoke-approval":
        return [{"edit": "revoke", "pkg": key, "witness": witness}]
    if change == "definition-changed":
        return [{"edit": "redefine", "pkg": key, "kind": kind, "witness": witness}]
    if change in ("org-deny", "org-enables-gate"):
        return [{"edit": "policy", "variant": "exec-deny-all", "witness": witness}]
    if change == "policy-dep-deny":
        return [{"edit": "policy", "variant": "deny-pkg", "witness": witness}]
    raise ValueError(change)


@dataclass(frozen=True)
class Cell:
    id: str  # the exact obligation id: G|<path>|<change>|<kind>
    smoke: bool = False
    xfail: str = ""
    known: tuple[Mapping[str, str], ...] = ()

    @property
    def parts(self) -> tuple[str, str, str]:
        _, path, change, kind = self.id.split("|")
        return path, change, kind


def build_row(cell: Cell) -> tuple[Row, str]:
    path, change, kind = cell.parts
    base, target = _KINDS[kind]
    suffix, scope, removal = _PATHS[path]
    key = base + suffix
    sibling = path == "uninstall-prune-rebuild"
    packages = (key, "p-skill") if sibling else (key,)
    witness = [f"{key}/{kind}/{target}"]
    given: list[object] = []
    if path == "marketplace-install":
        given.append("apm marketplace add MARKETPLACE_URL")
    if change == "org-enables-gate":
        given.append({"edit": "gate-off"})
    given.append(f"apm install {key}")
    if sibling:
        given.append("apm install p-skill")
    if change != "org-enables-gate":
        given += [f"apm approve {key}", "apm install"]
    when = [*_change(change, key, kind, witness), *removal]
    if change == "policy-dep-deny":
        when.append(_DENY_PKG_TAIL.format(key=key))
    row = Row(
        id=cell.id,
        promises=tuple(PROMISES),
        packages=packages,
        targets=(target,),
        scope="project",
        given=tuple(given),
        when=tuple(when),
        then={"deployed": "expected", "announce": _ANNOUNCE[change]},
        cell={"OP": path, "PRIM": kind, "GATE": change, "SCOPE": scope},
        known_cells=cell.known,
    )
    return row, scope


_NATIVE_APPROVE = "#4154 approve cannot find a parked native plugin"
CELLS = (
    Cell("G|install|revoke-approval|hook", smoke=True),
    Cell("G|install|local-deny|lsp", smoke=True),
    Cell("G|install|org-deny|mcp"),
    Cell("G|install|org-enables-gate|native"),
    Cell("G|install|policy-dep-deny|hook"),
    Cell("G|install|definition-changed|mcp"),
    Cell("G|frozen|org-deny|hook"),
    Cell("G|frozen|local-deny|mcp"),
    Cell("G|frozen|revoke-approval|lsp"),
    Cell("G|frozen|definition-changed|hook"),
    Cell("G|frozen|local-deny|native", xfail=_NATIVE_APPROVE),
    Cell("G|frozen|policy-dep-deny|mcp"),
    Cell("G|frozen|org-enables-gate|lsp"),
    Cell("G|update|definition-changed|hook", smoke=True),
    Cell("G|update|policy-dep-deny|mcp"),
    Cell("G|update|org-enables-gate|lsp"),
    Cell("G|update|revoke-approval|native", xfail=_NATIVE_APPROVE),
    Cell("G|update|local-deny|hook"),
    Cell("G|update|org-deny|lsp"),
    Cell("G|uninstall-prune-rebuild|org-enables-gate|hook"),
    Cell("G|uninstall-prune-rebuild|revoke-approval|mcp"),
    Cell("G|uninstall-prune-rebuild|org-deny|lsp"),
    Cell("G|uninstall-prune-rebuild|definition-changed|native", xfail=_NATIVE_APPROVE),
    Cell("G|uninstall-prune-rebuild|policy-dep-deny|hook"),
    Cell("G|uninstall-prune-rebuild|local-deny|lsp"),
    Cell("G|audit-ci|local-deny|hook"),
    Cell("G|audit-ci|org-enables-gate|mcp"),
    Cell("G|audit-ci|org-deny|lsp"),
    Cell("G|audit-ci|revoke-approval|native", xfail=_NATIVE_APPROVE),
    Cell("G|audit-ci|definition-changed|mcp"),
    Cell("G|audit-ci|policy-dep-deny|hook"),
    Cell("G|dry-run|local-deny|hook"),
    Cell("G|dry-run|org-deny|mcp"),
    Cell("G|dry-run|revoke-approval|lsp"),
    Cell("G|install-g|local-deny|hook"),
    Cell("G|install-g|definition-changed|mcp"),
    Cell("G|install-g|revoke-approval|lsp"),
    Cell("G|frozen-g|revoke-approval|hook"),
    Cell("G|frozen-g|local-deny|mcp"),
    Cell("G|frozen-g|definition-changed|hook"),
    Cell("G|bundle-install|local-deny|hook"),
    Cell("G|bundle-install|org-deny|mcp"),
    Cell("G|bundle-install|revoke-approval|hook"),
    Cell("G|bundle-install|org-enables-gate|mcp"),
    Cell("G|marketplace-install|policy-dep-deny|hook"),
    Cell("G|marketplace-install|revoke-approval|mcp"),
    Cell("G|marketplace-install|local-deny|hook"),
    Cell("G|marketplace-install|org-deny|mcp"),
    Cell("G|marketplace-install|org-enables-gate|hook"),
    Cell("G|native-plugin|local-deny|native", xfail=_NATIVE_APPROVE),
    Cell("X|install|definition-changed|canvas"),
    Cell("X|update|org-deny|canvas"),
    Cell("X|uninstall-prune-rebuild|local-deny|canvas"),
)


def _param(cell: Cell):
    marks = []
    if cell.smoke:
        marks.append(pytest.mark.lifecycle_smoke)
    if cell.xfail:
        marks.append(pytest.mark.xfail(strict=True, raises=AssertionError, reason=cell.xfail))
    return pytest.param(cell, id=cell.id, marks=marks)


@pytest.mark.parametrize("cell", [_param(cell) for cell in CELLS])
def test_gate_change_removes_on_the_next_mutating_command(lifecycle_world, cell: Cell) -> None:
    row, scope = build_row(cell)
    GateReplay(lifecycle_world, row, scope=scope).replay()


# -- lifecycle scripts: the execute path APM runs itself ---------------------

_SCRIPT_CHANGES = {
    "G|lifecycle-scripts|revoke-approval|script": ("apm lifecycle untrust",),
    "G|lifecycle-scripts|org-deny|script": ({"edit": "policy", "variant": "exec-deny-all"},),
    "X|lifecycle-scripts|definition-changed|script": ({"edit": "lifecycle", "tag": "v2"},),
}


@pytest.mark.parametrize("cell_id", sorted(_SCRIPT_CHANGES))
def test_lifecycle_script_gate(lifecycle_world, cell_id: str) -> None:
    """A trusted project script runs; after the change the next install runs none."""
    world = lifecycle_world
    row = Row(
        id=cell_id,
        promises=tuple(PROMISES),
        packages=("p-skill",),
        targets=("copilot",),
        scope="project",
        given=({"edit": "lifecycle"}, "apm lifecycle trust", "apm install p-skill"),
        when=(*_SCRIPT_CHANGES[cell_id], "apm install"),
        then={"deployed": "expected"},
    )
    GateReplay(world, row).replay()
    runs = _script_log(world).read_text("ascii").split()
    assert runs == ["v1"], f"script runs after the gate change: {runs}"
