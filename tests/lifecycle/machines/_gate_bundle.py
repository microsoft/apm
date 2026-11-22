"""M-GATE bundle spine: ``apm install <local bundle>`` at every gate level (#4095).

PROMISE: P-INV-I4 -- a deny, revoked approval or the gate turning on never
widens what a local bundle deploys; an approval of the exact bundle content
deploys its executables (source: tests/lifecycle/oracles.py).
PROMISE: P-OWN-executable-item-admission -- a local bundle's executables are
admitted by its exact-content approval key; its plain files deploy whatever
the gate decides (source: src/apm_cli/install/local_bundle_pending.py).
CELL: OP=bundle-install x PRIM x GATE (T1).

Two local bundles: ``p-bmix`` (a skill, an instruction, a hook and a stdio MCP
server, packed at run time with ``apm pack``) and the world's ``p-lsp`` bundle
(one LSP server). The spine installs both at five gate levels in turn:
unapproved, denied before any deploy, allowed (``apm approve <key>``), revoked
after deploy (``apm deny <key>``) and gate off. Every install runs I1-I9 and
must leave exactly the model's observable deployed set, the instruction file
where the gate keeps plain files, and the bundle's pending record while its
executables are parked. Under an org deny the spine also installs a copy of
p-bmix that ships ``bin/`` and no integrity manifest (another tool's bundle):
integrity is skipped, so the refusal is the policy's, and it credits bin/.
``bundle_cells`` labels the same script statically.
"""

from __future__ import annotations

import json
import shutil
import tarfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tests.lifecycle.machines._kit import Bench, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import (
    Evidence,
    ExpectedCell,
    check_obligation,
    partition_expected,
    strict_misses,
)
from tests.lifecycle.rows import _SURFACE_GLOBS
from tests.lifecycle.world.builder import _pack_bundle
from tests.lifecycle.world.packages import INERT_COMMAND
from tests.lifecycle.world.scopes import use_policy
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

MIX, LSP = "p-bmix", "p-lsp"
BUNDLES = (MIX, LSP)
# Credited kinds; ``other-file`` is the p-bmix instruction, checked by path.
KINDS: Mapping[str, frozenset[str]] = {
    MIX: frozenset({"skill", "other-file", "hook", "mcp", "canvas"}),
    LSP: frozenset({"lsp"}),
}
OBSERVED: Mapping[str, frozenset[str]] = {
    MIX: frozenset({"skill", "hook", "mcp", "canvas"}),
    LSP: frozenset({"lsp"}),
}
EXEC_TYPES: Mapping[str, tuple[str, ...]] = {MIX: ("canvas", "hooks", "mcp"), LSP: ("lsp",)}
# The org policy that denies every bundle by name (world policy variant).
BUNDLE_DENY_POLICY = "deny-all-deps"
_PLAIN = frozenset({"skill", "other-file"})
# Project paths a refused install must leave byte-identical.
_DURABLE = ("apm.yml", "apm.lock.yaml", "apm_modules", ".agents", ".github", ".mcp.json")
_INSTRUCTION = ".github/instructions/p-bmix.instructions.md"
_PENDING = Path("apm_modules") / ".local-bundle-pending.json"
_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo bmix"}]}]}}
# The p-bmix bundle with a bin/ tool and no apm.lock.yaml: only its refusal is credited.
_BIN_CELL = "G1|bundle|bin|denied-dependency|project"
_BIN_TOOL = Path("bin") / f"{MIX}-tool"


def author_bundle(root: Path, env: Mapping[str, str]) -> Path:
    """Author p-bmix and pack it as a local bundle (plugin format); return the archive."""
    factory = LocalPackageFactory(root / "src")
    server = {
        "name": f"{MIX}-mcp",
        "registry": False,
        "transport": "stdio",
        "command": INERT_COMMAND,
        "args": ["--stdio"],
    }
    package = factory.create(MIX, version="1.0.0", mcp_dependencies=[server])
    factory.add_instruction(package, MIX, '---\napplyTo: "**"\n---\n\nKeep the bundle mixed.\n')
    factory.add_hook(package, "session", _HOOK)
    factory.add_canvas(package, MIX, "export default function render() {\n  return 'p-bmix';\n}\n")
    factory.set_targets(package, ["copilot"])
    # A package with a .apm/ tree packs only .apm/ sources (plugin_exporter.py).
    skill = package.root / ".apm" / "skills" / MIX / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {MIX}\ndescription: Lifecycle bundle skill.\n---\n\nUse {MIX}.\n",
        encoding="ascii",
    )
    return _pack_bundle(package.root, root / "bundle", dict(env))


def lockless_bin_bundle(archive: Path, root: Path) -> Path:
    """Unpack *archive* under *root* as a bundle directory that ships bin/ and no lockfile.

    Without ``apm.lock.yaml`` install skips the integrity check (with a warning),
    so an org refusal of this bundle is the policy's, not the manifest's.
    """
    root.mkdir()
    # ``apm pack --archive`` writes a zip or a gzipped tarball depending on the release.
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle_zip:
            bundle_zip.extractall(root)  # noqa: S202 -- the spine's own apm pack output
    else:
        with tarfile.open(archive, mode="r:gz") as tar:
            tar.extractall(root, filter="data")
    (bundle,) = {path.parent for path in root.rglob("plugin.json")}
    (bundle / "apm.lock.yaml").unlink()
    tool = bundle / _BIN_TOOL
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\necho p-bmix\n", encoding="ascii")
    tool.chmod(0o755)
    return bundle


@dataclass
class BundleState:
    """The gate inputs the spine has set (pure)."""

    gate_on: bool = True
    decisions: dict[str, str] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    exec_deployed: set[str] = field(default_factory=set)
    denied_by_policy: bool = False

    def gate(self, bundle: str, kind: str) -> str:
        if self.denied_by_policy:
            return "denied-dependency"
        if kind in _PLAIN:
            return "revoked-after-deploy" if bundle in self.revoked else "none"
        if not self.gate_on:
            return "none"
        if bundle in self.revoked:
            return "revoked-after-deploy"
        decision = self.decisions.get(bundle)
        if decision == "allowed":
            return "allowed"
        return "denied-executable" if decision == "denied" else "unapproved-exec"

    # Bundle -> the gate level each executable kind had at that bundle's last
    # install: a bundle install reconciles only its own bundle (a side door).
    applied: dict[str, dict[str, str]] = field(default_factory=dict)

    def model(self, installing: str) -> ModelState:
        """What may be deployed right after ``apm install <installing>``."""
        allowed = {}
        for bundle in BUNDLES:
            for kind in KINDS[bundle] - _PLAIN:
                level = (
                    self.gate(bundle, kind)
                    if bundle == installing
                    else self.applied.get(bundle, {}).get(kind, "unapproved-exec")
                )
                if level in ("none", "allowed"):
                    allowed[(bundle, kind)] = "allowed"
        installed = {installing, *self.applied}
        return ModelState(declared=frozenset(installed), decisions=allowed)


class GateBundles(StepRunner):
    """Run (or, with no bench, only label) the bundle spine at project scope."""

    def __init__(self, bench: Bench | None) -> None:
        super().__init__(bench, ModelState(), OBSERVED)
        self.dry = bench is None
        self.state = BundleState()
        self.credited: set[str] = set()
        self.keys: dict[str, str] = {}
        if bench is not None:
            self.archives = {
                MIX: author_bundle(bench.world.root / "gate-bundle", bench.world.env),
                LSP: Path(bench.world.packages["p-lsp:bundle"].spec),
            }

    def cells(self, bundle: str) -> list[str]:
        return sorted(
            f"G1|bundle|{kind}|{self.state.gate(bundle, kind)}|project" for kind in KINDS[bundle]
        )

    # -- commands ----------------------------------------------------------------

    def install(self, bundle: str) -> None:
        """``apm install <bundle>``: check I1-I9 and the bundle's outcome, then credit."""
        cells = self.cells(bundle)
        self.credited.update(cells)
        if self.dry:
            self._advance(bundle)
            return
        if self.state.denied_by_policy:
            self._refused(bundle, cells)
            return
        step = self.step(
            ("install", str(self.archives[bundle])),
            exits={0},
            cells=cells,
            model=self.state.model(bundle),
            mutating=True,
        )
        self.check_outcome(bundle, step)
        present, absent = self._plain_evidence(bundle)
        evidence = Evidence(unchanged={"outside": ("*",)}, present=present, absent=absent, exit=0)
        for cell in cells:
            check_obligation(
                cell,
                strength="e2e-evidence",
                before=step.before,
                after=step.after,
                result=step.result,
                evidence=evidence,
            )
        self._advance(bundle)

    def _refused(self, bundle: str, cells: list[str]) -> None:
        """Under an org dependency deny the bundle install refuses and changes nothing."""
        step = self.step(
            ("install", str(self.archives[bundle])), exits={1}, cells=cells, mutating=False
        )
        output = step.result.stdout + step.result.stderr
        assert "Install blocked by org policy" in output, output
        evidence = Evidence(
            unchanged={"project": _DURABLE, "outside": ("*",)},
            output_matches=(r"Policy violation: \S*" + bundle,),
            exit=1,
        )
        for cell in cells:
            check_obligation(
                cell,
                strength="e2e-evidence",
                before=step.before,
                after=step.after,
                result=step.result,
                evidence=evidence,
            )

    def install_lockless_bin(self) -> None:
        """Under the org deny, the p-bmix copy shipping bin/ and no manifest is refused too."""
        assert self.state.denied_by_policy
        self.credited.add(_BIN_CELL)
        if self.dry:
            return
        root = self.bench.world.root / "gate-bundle-bin"
        bundle = lockless_bin_bundle(self.archives[MIX], root)
        try:
            step = self.step(("install", str(bundle)), exits={1}, cells=[_BIN_CELL], mutating=False)
            output = step.result.stdout + step.result.stderr
            assert "skipping integrity check" in output, output
            assert "Install blocked by org policy" in output, output
            assert (bundle / _BIN_TOOL).is_file()
            evidence = Evidence(
                unchanged={"project": _DURABLE, "outside": ("*",)},
                output_matches=(r"Policy violation: \S*" + MIX,),
                exit=1,
            )
            check_obligation(
                _BIN_CELL,
                strength="e2e-evidence",
                before=step.before,
                after=step.after,
                result=step.result,
                evidence=evidence,
            )
        finally:
            shutil.rmtree(root)

    def deny_by_policy(self) -> None:
        """The org policy denies both bundles by name (an input edit)."""
        if not self.dry:
            self.edit(
                "policy-deny-bundles", lambda: use_policy(self.bench.world, BUNDLE_DENY_POLICY)
            )
        self.state.denied_by_policy = True

    def _advance(self, bundle: str) -> None:
        self.state.applied[bundle] = {k: self.state.gate(bundle, k) for k in KINDS[bundle] - _PLAIN}
        if all(self.state.gate(bundle, k) in ("none", "allowed") for k in KINDS[bundle] - _PLAIN):
            self.state.exec_deployed.add(bundle)
        else:
            self.state.exec_deployed.discard(bundle)

    def _plain_evidence(self, bundle: str) -> tuple[dict, dict]:
        if bundle != MIX:
            return {}, {}
        return {"project": [_INSTRUCTION, f".agents/skills/{MIX}/SKILL.md"]}, {}

    def check_outcome(self, bundle: str, step: Step) -> None:
        """Exactly the model's observable set is deployed; parked executables are pending."""
        wanted = {
            "/".join(cell)
            for cell in expected_deployed(self.state.model(bundle), OBSERVED)
            if (cell[1], cell[2]) in _SURFACE_GLOBS
        }
        assert set(step.after.deployed) == wanted, (
            f"deployed {sorted(step.after.deployed)}, model {sorted(wanted)}"
        )
        parked = [k for k in KINDS[bundle] - _PLAIN if self.state.gate(bundle, k) != "allowed"]
        record = self.bench.manifest_dir / _PENDING
        bundles = (
            json.loads(record.read_text(encoding="utf-8")).get("bundles") or {}
            if record.is_file()
            else {}
        )
        entry = next(
            (
                e
                for e in bundles.values()
                if str(e.get("approval_key", "")).startswith(bundle + "#")
            ),
            None,
        )
        if parked and self.state.gate_on:
            assert entry is not None, f"{bundle}: parked executables are not pending: {bundles}"
            assert set(EXEC_TYPES[bundle]) <= set(entry["exec_types"]), entry
            self.keys[bundle] = entry["approval_key"]

    def decide(self, verdict: str) -> None:
        """``apm approve|deny <key>`` for both bundles (an input to the next installs)."""
        for bundle in BUNDLES:
            if not self.dry:
                result = self.bench.run((verdict, self.keys[bundle]))
                assert result.returncode == 0, result.stdout + result.stderr
            self.state.decisions[bundle] = "allowed" if verdict == "approve" else "denied"
            if verdict == "deny" and bundle in self.state.exec_deployed:
                self.state.revoked.add(bundle)
            if verdict == "approve":
                self.state.revoked.discard(bundle)

    def gate_off(self) -> None:
        def change() -> None:
            data = self.bench.manifest()
            data.pop("executables", None)
            self.bench.write_manifest(data)

        if not self.dry:
            self.edit("gate-off", change)
        self.state.gate_on = False
        self.state.decisions.clear()


def replay_bundles(machine: GateBundles) -> None:
    """Install both bundles at five gate levels; each level's installs earn its ids."""

    def both() -> None:
        for bundle in BUNDLES:
            machine.install(bundle)

    both()  # unapproved: the world's gate is on, nothing approved
    machine.decide("deny")
    both()  # denied before any deploy
    machine.decide("approve")
    both()  # allowed
    machine.decide("deny")
    both()  # revoked after deploy: executables retire, plain files stay
    machine.gate_off()
    both()  # no layer opts into the gate
    machine.deny_by_policy()
    both()  # the org denies the bundles: each install refuses, nothing changes
    machine.install_lockless_bin()  # ...and so does a p-bmix copy that ships bin/


def bundle_cells() -> frozenset[str]:
    """Every machine id the bundle spine credits (labels only, no CLI)."""
    machine = GateBundles(None)
    machine.keys = dict.fromkeys(BUNDLES, "")
    replay_bundles(machine)
    return frozenset(machine.credited)


# -- pack: what a project bundles of its gated dependencies -----------------------


@dataclass(frozen=True)
class PackSet:
    """The git dependencies one pack project declares, and what pack ships of them."""

    kinds: Mapping[str, frozenset[str]]
    # Gate verdicts name only the packages that declare executables.
    decided: tuple[str, ...]
    # The bundle paths each deployed dependency executable packs to.
    executables: Mapping[str, tuple[str, ...]]
    # Plain dependency files ship at every gate level.
    plain: tuple[str, ...] = ()
    # Publishes the set's only dependency in the world and returns its spec; None
    # means world git packages (``<name>:git``).
    serve: Callable[[object], str] | None = None

    @property
    def packed(self) -> tuple[str, ...]:
        return tuple(self.kinds)


# Pack never ships an MCP server ``apm install`` manages (only servers apm.yml defines).
EXEC_PACK = PackSet(
    kinds={
        "p-exec": frozenset({"hook", "mcp"}),
        "p-lsp": frozenset({"lsp"}),
        "p-skill": frozenset({"skill"}),
    },
    decided=("p-exec", "p-lsp"),
    executables={"hook": ("hooks/p-exec-session.json",), "lsp": (".lsp.json",)},
    plain=("skills/p-skill/SKILL.md",),
)
# A canvas packs in a project of its own, so a canvas failure never hides the other
# kinds' cells (a parked canvas failed the whole pack until #4714 was fixed).
CANVAS_PACK = PackSet(
    kinds={"p-canvas": frozenset({"canvas"})},
    decided=("p-canvas",),
    executables={"canvas": ("extensions/p-canvas/extension.mjs", "extensions/p-canvas/icon.txt")},
)
_MIXED = "p-mixgit"
_MIXED_HOOK = {
    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo p-mixgit"}]}]}
}


def _served_mixed(world) -> str:
    """Publish ``lifecycle/p-mixgit`` (a skill beside a hook) from git; pack refuses
    local paths, and no world git package pairs a plain file with an executable."""
    factory = LocalPackageFactory(world.root / "pack-mixed")
    package = factory.create(_MIXED, version="1.0.0")
    skill = f"---\nname: {_MIXED}\ndescription: A skill beside a hook.\n---\n\nUse {_MIXED}.\n"
    factory.add_skill(package, _MIXED, skill)
    factory.add_hook(package, "session", _MIXED_HOOK)
    repos = LocalGitRepositoryFactory(world.root / "pack-mixed-repos", env=world.env)
    repository = repos.create(_MIXED, source_tree=package.root)
    repos.tag(repository, "v1.0.0", repos.commit(repository, message=f"{_MIXED} v1.0.0"))
    world.services.serve(f"lifecycle/{_MIXED}", repository.origin)
    return f"lifecycle/{_MIXED}#v1.0.0"


# A skill beside a hook: its plain file reads the hook's gate level (U2), so this set
# earns the plain-file pack ids at every level, revoked-after-deploy included.
MIX_PACK = PackSet(
    kinds={_MIXED: frozenset({"skill", "hook"})},
    decided=(_MIXED,),
    executables={"hook": (f"hooks/{_MIXED}-session.json",)},
    plain=(f"skills/{_MIXED}/SKILL.md",),
    serve=_served_mixed,
)
PACK_SETS = (EXEC_PACK, CANVAS_PACK, MIX_PACK)
# Primitives a package ships that pack never deploys at project scope (U-BIN).
_SHIPS_ALSO: Mapping[str, tuple[str, ...]] = {"p-exec": ("bin",)}
# Strict known cells (R: pack must exit 0 and ship what the gate deploys); none
# since #4714 closed.
KNOWN_PACK: dict[str, str] = {}
_KNOWN_PACK_MATCH = r"pack exited 1: .*Cannot pack dependency lifecycle/p-canvas"


class GatePack(StepRunner):
    """``apm pack --dry-run`` over git dependencies at five gate levels (project scope)."""

    def __init__(self, bench: Bench | None, packs: PackSet = EXEC_PACK) -> None:
        super().__init__(bench, ModelState(), packs.kinds)
        self.packs = packs
        self.dry = bench is None
        self.level = "unapproved-exec"
        self.credited: set[str] = set()

    def specs(self) -> tuple[str, ...]:
        if self.packs.serve is not None:
            return (self.packs.serve(self.bench.world),)
        return tuple(self.bench.world.packages[f"{name}:git"].spec for name in self.packs.packed)

    def level_model(self) -> ModelState:
        allowed = self.level in ("none", "allowed")
        decisions = {
            (name, kind): "allowed"
            for name in self.packs.decided
            for kind in self.packs.kinds[name]
            if allowed
        }
        return ModelState(declared=frozenset(self.packs.packed), decisions=decisions)

    def install(self, *names: str) -> None:
        if not self.dry:
            self.step(("install", *names), exits={0}, model=self.level_model(), mutating=True)

    def decide(self, verdict: str, level: str) -> None:
        if not self.dry:
            names = (f"lifecycle/{name}" for name in self.packs.decided)
            result = self.bench.run((verdict, *names))
            assert result.returncode == 0, result.stdout + result.stderr
        self.level = level

    def gate_off(self) -> None:
        def change() -> None:
            data = self.bench.manifest()
            data.pop("executables", None)
            self.bench.write_manifest(data)

        if not self.dry:
            self.edit("gate-off", change)
        self.level = "none"

    def pack(self) -> None:
        """Pack ships exactly the executables the gate lets the project deploy."""
        found = {
            f"G1|pack|{kind}|{self._level(name, kind)}|project"
            for name in self.packs.packed
            for kind in self.packs.kinds[name]
        }
        if self.level == "denied-dependency":
            # The refusal covers every primitive a denied package ships, its bin/ too.
            found |= {
                f"G1|pack|{kind}|{self.level}|project"
                for name in self.packs.packed
                for kind in _SHIPS_ALSO.get(name, ())
            }
        cells = sorted(found)
        self.credited.update(cells)
        if self.dry:
            return
        if self.level == "denied-dependency":
            self._refused(cells)
            return
        step = self.step(
            ("pack", "--dry-run"), exits={0, 1}, cells=cells, read_only=True, mutating=False
        )
        known = self._reference(step, cells)
        listed = {line.strip() for line in step.result.stdout.splitlines()}
        shipped = self.level in ("none", "allowed")
        for path in (p for paths in self.packs.executables.values() for p in paths):
            assert known or (path in listed) == shipped, f"{self.level}: {path} in {listed}"
        for path in self.packs.plain:
            assert path in listed, f"{self.level}: plain {path} not packed:\n{step.result.stdout}"
        assert not any("mcp" in line.lower() and line.endswith(".json") for line in listed), listed
        for cell in cells:
            if cell in KNOWN_PACK:
                continue
            check_obligation(
                cell,
                strength="e2e-evidence",
                before=step.before,
                after=step.after,
                result=step.result,
                evidence=Evidence(unchanged={"project": ("*",), "outside": ("*",)}, exit=0),
            )

    def _level(self, name: str, kind: str) -> str:
        """The GATE value of one packed (package, kind): a plain file of a package
        the spine never decided has no executable beside it, so no gate level (U2);
        the org dependency deny covers every package."""
        if kind in _PLAIN and name not in self.packs.decided and self.level != "denied-dependency":
            return "none"
        return self.level

    def _refused(self, cells: list[str]) -> None:
        """Under an org dependency deny pack refuses and ships nothing."""
        step = self.step(
            ("pack", "--dry-run"), exits={1}, cells=cells, read_only=True, mutating=False
        )
        evidence = Evidence(
            unchanged={"project": ("*",), "outside": ("*",)},
            output_matches=(r"Pack blocked by org policy", r"Nothing was packed"),
            exit=1,
        )
        for cell in cells:
            check_obligation(
                cell,
                strength="e2e-evidence",
                before=step.before,
                after=step.after,
                result=step.result,
                evidence=evidence,
            )

    def deny_by_policy(self) -> None:
        """The org policy denies every dependency of the pack project (an input edit)."""
        if not self.dry:
            self.edit("policy-deny-deps", lambda: use_policy(self.bench.world, BUNDLE_DENY_POLICY))
        self.level = "denied-dependency"

    def _reference(self, step: Step, cells: list[str]) -> bool:
        """R: pack exits 0; a strict known cell must fail on its step, and only it."""
        result = step.result
        output = f"{result.stdout}\n{result.stderr}"
        error = next((line for line in output.splitlines() if "Error" in line), "")
        problems = [f"pack exited {result.returncode}: {error}"] if result.returncode else []
        expected = [ExpectedCell(cell, _KNOWN_PACK_MATCH) for cell in cells if cell in KNOWN_PACK]
        unexpected, owners = partition_expected(problems, expected)
        misses = strict_misses(expected, owners)
        assert not unexpected and not misses, (
            f"R: pack at {self.level}: {unexpected}; known cells that passed: {misses}\n"
            f"{result.stdout}\n{result.stderr}"
        )
        self.expected_seen.update(owners)
        return bool(owners)


def replay_pack(machine: GatePack) -> None:
    """Pack at each gate level, after an install has reconciled that level."""
    machine.install(*(() if machine.dry else machine.specs()))
    machine.pack()  # unapproved
    machine.decide("deny", "denied-executable")
    machine.install()
    machine.pack()
    machine.decide("approve", "allowed")
    machine.install()
    machine.pack()
    machine.decide("deny", "revoked-after-deploy")
    machine.install()
    machine.pack()
    machine.gate_off()
    machine.install()
    machine.pack()
    machine.deny_by_policy()
    machine.pack()  # the org denies every dependency: pack refuses, ships nothing


def pack_cells() -> frozenset[str]:
    credited: set[str] = set()
    for packs in PACK_SETS:
        machine = GatePack(None, packs)
        replay_pack(machine)
        credited |= machine.credited
    return frozenset(credited)
