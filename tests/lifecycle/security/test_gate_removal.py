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

Pairwise coverage over KIND x PATH x CHANGE; every applicable (KIND, PATH)
pair appears once. Not applicable, so no cell:

| KIND / PATH | why |
|---|---|
| bin, every path | bin/ deploys only for marketplace plugins to Claude at user scope |
|                 | (integration/skill_integrator.py _deploy_plugin_bin); no world    |
|                 | package ships one (p-exec is an APM package, natives have no bin) |
| canvas, user    | Copilot has no user-scope canvas surface (integration/targets.py) |
| native, user    | approve cannot find a parked native plugin at user scope (#4154)  |
| lsp/canvas/native, bundle or marketplace | the world packs and lists p-exec only |
| org changes, user | org policy governs project installs only                       |
| policy denies dep, not p-exec | the deny-pkg variant denies p-exec only          |

Lifecycle scripts (the execute path APM runs itself) have no deployed file;
their cells count script runs instead (``test_lifecycle_script_gate``).
"""

from __future__ import annotations

import json
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
            assert str(then["announce"]) in output, f"I7: not announced: {then['announce']!r}"

    # -- one CLI step ----------------------------------------------------

    def _fields(self, args: tuple[str, ...], read_only: bool) -> dict[str, object]:
        if self.scope == "project":
            fields: dict[str, object] = {
                "forbidden": {"home": ("*",)},
                "allowed": {"home": _PROJECT_HOME_ALLOWED},
            }
        else:
            fields = {"forbidden": {"project": ("*",)}, "allowed": {}}
        fields["read_only"] = read_only
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
        if self.ceiling is not None and op.command in _GATE_REMOVING:
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


@dataclass(frozen=True)
class Kind:
    key: str
    target: str = "copilot"
    approve: str = ""

    def surface(self, kind: str) -> str:
        return f"{self.key}/{kind}/{self.target}"


_KINDS = {
    "hook": Kind("p-exec"),
    "mcp": Kind("p-exec"),
    "lsp": Kind("p-lsp"),
    "canvas": Kind("p-canvas"),
    "native": Kind("p-native-claude", target="claude"),
}
# What each CHANGE does as steps, given the package key and the kind.
_ORG_VARIANT = {"mcp": "mcp-deny"}


def _change(change: str, key: str, kind: str, witness: list[str]) -> list[object]:
    if change == "deny":
        return [{"edit": "mark", "witness": witness}, f"apm deny {key}"]
    if change == "revoke":
        return [{"edit": "revoke", "pkg": key, "witness": witness}]
    if change == "redefine":
        return [{"edit": "redefine", "pkg": key, "kind": kind, "witness": witness}]
    if change in ("org-deny", "org-enables-gate"):
        variant = _ORG_VARIANT.get(kind, "exec-deny-all")
        if change == "org-enables-gate":
            variant = "exec-deny-all"
        return [{"edit": "policy", "variant": variant, "witness": witness}]
    if change == "policy-denies-dep":
        return [{"edit": "policy", "variant": "deny-pkg", "witness": witness}]
    raise ValueError(change)


# PATH -> the steps that must remove the surface, after the change.
_PATHS: Mapping[str, list[object]] = {
    "install": ["apm install"],
    "install-frozen": ["apm install --frozen"],
    "update": ["apm update --yes"],
    "uninstall": ["apm uninstall p-skill"],
    "prune": ["apm prune"],
    "user": ["apm install"],
    "bundle": ["apm install"],
    "marketplace": ["apm install"],
    "audit-replay": [{"pair": ["apm audit --ci", "apm install --frozen"]}],
}
_PATH_KEY = {"bundle": ":bundle", "marketplace": ":marketplace"}
# A removing step under deny-pkg resolves the denied package and refuses (exit 1,
# state kept); the user then uninstalls it, the next step that can succeed.
_DENY_PKG_TAIL = ["apm uninstall {key}"]
_ANNOUNCE = {
    "deny": "denied",
    "revoke": "not approved",
    "redefine": "definition changed since approval",
    "org-deny": "denied",
    "org-enables-gate": "denied",
    "policy-denies-dep": "denied by pattern",
}


@dataclass(frozen=True)
class Cell:
    kind: str
    path: str
    change: str
    smoke: bool = False
    known: tuple[Mapping[str, str], ...] = ()
    xfail: str = ""

    @property
    def id(self) -> str:
        return f"{self.kind}-{self.path}-{self.change}"


def build_row(cell: Cell) -> tuple[Row, str]:
    spec = _KINDS[cell.kind]
    key = spec.key + _PATH_KEY.get(cell.path, "")
    scope = "user" if cell.path == "user" else "project"
    packages = [key] if cell.path not in ("uninstall", "prune") else [key, "p-skill"]
    witness = [f"{key}/{cell.kind}/{spec.target}"]
    given: list[object] = []
    if cell.change == "org-enables-gate":
        given.append({"edit": "gate-off"})
    given.append(f"apm install {key}")
    if cell.path in ("uninstall", "prune"):
        given.append("apm install p-skill")
    if cell.change != "org-enables-gate":
        given += [f"apm approve {key}", "apm install"]
    when = _change(cell.change, key, cell.kind, witness)
    removal = list(_PATHS[cell.path])
    if cell.change == "policy-denies-dep":
        removal = [
            {"run": s, "exit": 1} if isinstance(s, str) and s.split()[1] in _RECONCILING else s
            for s in removal
        ]
        removal += [line.format(key=key) for line in _DENY_PKG_TAIL]
    when += removal
    row = Row(
        id=f"T-7a/gate-removal/{cell.id}",
        promises=tuple(PROMISES),
        packages=tuple(packages),
        targets=(spec.target,),
        scope="project",
        given=tuple(given),
        when=tuple(when),
        then={"deployed": "expected", "announce": _ANNOUNCE[cell.change]},
        cell={"OP": cell.path, "PRIM": cell.kind, "GATE": cell.change, "SCOPE": scope},
        known_cells=cell.known,
    )
    return row, scope


# Pairwise: every applicable (KIND, PATH) once; CHANGE rotates so every
# (KIND, CHANGE) and (PATH, CHANGE) pair the world supports appears.
CELLS = (
    Cell("hook", "install", "revoke", smoke=True),
    Cell("mcp", "install", "org-deny"),
    Cell("lsp", "install", "deny", smoke=True),
    Cell("canvas", "install", "redefine"),
    Cell("native", "install", "org-enables-gate"),
    Cell("hook", "install-frozen", "org-deny"),
    Cell("mcp", "install-frozen", "deny"),
    Cell("lsp", "install-frozen", "redefine"),
    Cell("canvas", "install-frozen", "revoke"),
    Cell("native", "install-frozen", "deny"),
    Cell("hook", "update", "redefine", smoke=True),
    Cell("mcp", "update", "policy-denies-dep"),
    Cell("lsp", "update", "org-enables-gate"),
    Cell("canvas", "update", "org-deny"),
    Cell("native", "update", "revoke"),
    Cell("hook", "uninstall", "org-enables-gate"),
    Cell("mcp", "uninstall", "revoke"),
    Cell("lsp", "uninstall", "org-deny"),
    Cell("canvas", "uninstall", "deny"),
    Cell("native", "uninstall", "redefine"),
    Cell("hook", "prune", "policy-denies-dep"),
    Cell("mcp", "prune", "redefine"),
    Cell("lsp", "prune", "revoke"),
    Cell("canvas", "prune", "org-enables-gate"),
    Cell("native", "prune", "org-deny"),
    Cell("hook", "user", "deny"),
    Cell("mcp", "user", "redefine"),
    Cell("lsp", "user", "revoke"),
    Cell("hook", "bundle", "deny"),
    Cell("mcp", "bundle", "org-deny"),
    Cell("hook", "marketplace", "policy-denies-dep"),
    Cell("mcp", "marketplace", "revoke"),
    Cell("hook", "audit-replay", "deny"),
    Cell("mcp", "audit-replay", "org-enables-gate"),
    Cell("lsp", "audit-replay", "org-deny"),
    Cell("canvas", "audit-replay", "redefine"),
    Cell("native", "audit-replay", "revoke"),
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
    "revoke": ["apm lifecycle untrust"],
    "redefine": [{"edit": "lifecycle", "tag": "v2"}],
    "org-deny": [{"edit": "policy", "variant": "exec-deny-all"}],
}


@pytest.mark.parametrize("change", sorted(_SCRIPT_CHANGES))
def test_lifecycle_script_gate(lifecycle_world, change: str) -> None:
    """A trusted project script runs; after the change the next install runs nothing."""
    world = lifecycle_world
    row = Row(
        id=f"T-7a/gate-removal/script-install-{change}",
        promises=tuple(PROMISES),
        packages=("p-skill",),
        targets=("copilot",),
        scope="project",
        given=({"edit": "lifecycle"}, "apm lifecycle trust", "apm install p-skill"),
        when=(*_SCRIPT_CHANGES[change], "apm install"),
        then={"deployed": "expected"},
    )
    replay = GateReplay(world, row)
    replay.replay()
    runs = _script_log(world).read_text("ascii").split()
    assert runs == ["v1"], f"script runs after the gate change: {runs}"
