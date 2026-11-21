"""M-SOURCE: every dependency form through every resolving command (#4096).

PROMISE: P-INV-I1..I9 -- every install, update, uninstall, prune, compile and
lock step keeps the cross-cutting laws, whatever form declared the package
(source: tests/lifecycle/oracles.py).
PROMISE: P-OWN-prospective-dry-run-install-plan -- ``--dry-run`` writes nothing
and names every package the real command then changes (source:
src/apm_cli/install/dry_run_plan.py; docs: "Show what would change without
writing files").
CELL: OP x SRC x PRIM (T3, ``t3_cells``) and DRYRUN x OP x SRC (T5, ``t5_cells``).

Forms: shorthand, git object (any host shape), local path (diamond parents,
LSP, canvas), marketplace object, ``plugin@marketplace``, local bundle, native
Claude plugin, package registry object (experimental ``registries``), MCP
registry server and inline MCP server. ``apm lock`` has no ``--dry-run`` flag,
so its T5 cells are ``real`` only; the local bundle is a side door with no
manifest entry, so only ``install`` applies to it (update, prune and uninstall
of a bundle are U8 rule cells of the covering replay); an MCP server
is removed by editing ``apm.yml`` (``apm uninstall`` refuses it: a U8 refusal
cell of the covering replay).

Context ids ``X|<op>|<form>|<FACTOR>=<value>`` (``X_CONTEXT``) name the state a
real step ran in: scope, network, target change, gate, a clean prior
deployment and the ``--force``, ``--update``, ``--frozen`` and ``--target``
modifiers. A step earns one only when its form is acted on in that state, and
the generator reads it as the assignment {OP, SRC, FACTOR}.

A dry-run step must leave every durable byte unchanged (I5 read-only); the real
command then runs and every package whose lock row, deployed skill or MCP entry
it changed must be named in the preview. The preview lists every locally known
dependency, so the comparison is ``real <= preview``; when the preview declares
itself incomplete ("Orphan removals are not previewed") removed rows are exempt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import initialize, rule, run_state_machine_as_test

from tests.lifecycle.machines._kit import (
    Bench,
    KnownCell,
    MachineBase,
    Step,
    WorldPool,
    earned_cells,
    machine_settings,
)
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import UNGATED_KINDS, ModelState
from tests.lifecycle.oracles import Evidence, Observation, check_obligation
from tests.lifecycle.receipts import current_node
from tests.lifecycle.rows import _PROJECT_HOME_ALLOWED, _SURFACE_GLOBS
from tests.lifecycle.world import registry
from tests.lifecycle.world.packages import INERT_COMMAND
from tests.lifecycle.world.services import (
    API_HOST,
    GITHUB_HOST,
    HOST_ALIASES,
    MARKETPLACE_NAME,
    RAW_HOST,
    REGISTRY_HOST,
    REGISTRY_SERVER,
)


@pytest.fixture(autouse=True)
def lifecycle_output_oracle():
    """Replace the global output hook: every machine step already checks I6, I8 and I9
    through ``check_cross_cutting``, which also honors the strict known cells below."""
    yield


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
        "P-OWN-prospective-dry-run-install-plan",
        "P-OWN-dependency-identity-materialization",
        "P-OWN-effective-install-target-selection",
        "P-OWN-host-reference-coordinates",
    ),
]

OPS = ("install", "update", "uninstall", "prune", "compile", "lock")
FORMS = (
    "shorthand",
    "git-object",
    "local-path",
    "marketplace-object",
    "plugin-at-mkt",
    "bundle",
    "native",
    "pkg-registry",
    "mcp-registry",
    "inline-mcp",
)
SHAPES = ("github", *HOST_ALIASES)
TARGET_SETS = (("copilot",), ("copilot", "claude"), ("claude",))
NETWORK = ("online", "offline", "proxy-only")
REF = "v1.0.0"
MARKET = MARKETPLACE_NAME
WORLD_HOSTS = frozenset(
    {
        GITHUB_HOST,
        API_HOST,
        RAW_HOST,
        REGISTRY_HOST,
        registry.HOST,
        *(host for host, _ in HOST_ALIASES.values()),
    }
)
# A self-defined MCP server (``install --mcp NAME -- <command>``); its command never runs.
INLINE_SERVER = "p-inline"
# MCP server slots -> the server name their ``dependencies.mcp`` entry carries.
_MCP_SERVERS = {"p-registry": REGISTRY_SERVER, INLINE_SERVER: INLINE_SERVER}
# The exact names a configured or locked entry of each slot may carry (a registry
# server is keyed by its full id or by its short name).
_MCP_NAMES = {
    "p-registry": frozenset({REGISTRY_SERVER, REGISTRY_SERVER.rsplit("/", 1)[-1]}),
    INLINE_SERVER: frozenset({INLINE_SERVER}),
}
_CLOSED_PROXY = "http://127.0.0.1:9"
_NO_DRY_RUN = frozenset({"lock"})
# Ops that do not resolve: they may keep what is deployed (``apm lock`` never deletes).
_KEEPING = frozenset({"lock", "compile"})
_MARKET_CACHE = ".apm/cache/marketplace"
_PINS = f"{_MARKET_CACHE}/version-pins.json"
# The marketplace catalog cache a project install may refresh under APM_HOME.
_CATALOG_CACHE = (
    f"{_MARKET_CACHE}/{MARKETPLACE_NAME}.json",
    f"{_MARKET_CACHE}/{MARKETPLACE_NAME}.meta.json",
)
# What a package change is: its resolved content and deployment, not graph
# bookkeeping such as ``resolved_by`` (a diamond child re-parented on uninstall).
_ROW_CONTENT = ("resolved_commit", "version", "content_hash", "deployed_file_hashes")
_INCOMPLETE = re.compile(r"Orphan removals are not previewed|Dry run incomplete")
_PREVIEW_LINE = re.compile(r"^\s*\[[i!+\-~]\]\s+(?:-\s+)?(\S+)")


@dataclass(frozen=True)
class Choice:
    """One dependency form and the world package it declares.

    ``kinds`` are what the oracle observes; ``prims`` are the primitives the
    package carries, which name its T3 cells (a native plugin is observed as
    one gated ``native`` unit but carries a skill and an MCP server or bin/).
    """

    form: str
    package: str
    kinds: frozenset[str]
    carries: frozenset[str] | None = None

    @property
    def prims(self) -> frozenset[str]:
        return self.kinds if self.carries is None else self.carries

    @property
    def slot(self) -> str:
        """Forms of one identity share a slot: declaring one replaces the other."""
        if self.package == "p-skill":
            return "remote-skill"
        return "remote-exec" if self.package == "p-exec" else self.package


_SKILL = frozenset({"skill"})
_EXEC = frozenset({"hook", "bin", "mcp"})
_NATIVE = frozenset({"native"})
CHOICES = (
    Choice("shorthand", "p-skill", _SKILL),
    Choice("git-object", "p-skill", _SKILL),
    Choice("marketplace-object", "p-skill", _SKILL),
    Choice("plugin-at-mkt", "p-exec", _EXEC),
    Choice("bundle", "p-exec-bundle", _EXEC),
    Choice("bundle", "p-lsp-bundle", frozenset({"lsp"})),
    Choice("local-path", "p-parent", _SKILL),
    Choice("local-path", "p-parent2", _SKILL),
    Choice("local-path", "p-lsp", frozenset({"lsp"})),
    Choice("local-path", "p-canvas", frozenset({"canvas"})),
    Choice("native", "p-native-claude", _NATIVE, frozenset({"skill", "mcp"})),
    Choice("native", "p-native-claude-bin", _NATIVE, frozenset({"skill", "bin"})),
    Choice("pkg-registry", "p-skill", _SKILL),
    Choice("pkg-registry", "p-exec", _EXEC),
    Choice("mcp-registry", "p-registry", frozenset({"mcp"})),
    Choice("inline-mcp", INLINE_SERVER, frozenset({"mcp"})),
)
_BY_FORM = {choice.form: choice for choice in CHOICES}
_REGISTRY_SKILL, _REGISTRY_EXEC = (c for c in CHOICES if c.form == "pkg-registry")
_MCP_FORMS = frozenset({"mcp-registry", "inline-mcp"})
_CLI_FORMS = frozenset({"plugin-at-mkt", "bundle", *_MCP_FORMS})
_DIAMOND = frozenset({"p-parent", "p-parent2"})
# Observed packages and their kinds (registry and bundle deploy no observed surface).
KINDS: Mapping[str, frozenset[str]] = {
    "p-skill": _SKILL,
    "p-exec": _EXEC,
    "p-child": _SKILL,
    # A native plugin is gated as one unit; its carried skill and MCP server are observed
    # too, so an unapproved native deployment is an I4 violation (bin/ has no surface here).
    **{c.package: c.kinds | c.prims for c in CHOICES if c.form in ("local-path", "native")},
}
_NAMES = sorted({*KINDS, "p-registry", INLINE_SERVER}, key=len, reverse=True)


def applies(op: str, form: str) -> bool:
    """Whether *op* acts on a dependency of *form* (see the module docstring)."""
    if form == "bundle":
        return op == "install"
    return not (form in _MCP_FORMS and op == "uninstall")


def t3_cells() -> frozenset[str]:
    """OP x SRC x PRIM obligation cells."""
    return frozenset(
        f"T3|{op}|{choice.form}|{kind}"
        for op in OPS
        for choice in CHOICES
        if applies(op, choice.form)
        for kind in choice.prims
    )


def t5_cells() -> frozenset[str]:
    """DRYRUN x OP x SRC obligation cells."""
    return frozenset(
        f"T5|{mode}|{op}|{form}"
        for op in OPS
        for form in FORMS
        if applies(op, form)
        for mode in (("real",) if op in _NO_DRY_RUN else ("dry-run", "real"))
    )


_COMMON = (
    "SCOPE=project",
    "NET=online",
    "TGT=single-unchanged",
    "TGT=multi-unchanged",
    "SELECT=on",
    "FORCE=on",
    "UPDATE=on",
)
_UNAPPROVED = "GATE=unapproved-exec"
# GATE=none: the gate-off pass (no ``executables`` block, the default user path).
_MANIFEST_CONTEXT = (*_COMMON, _UNAPPROVED, "GATE=none", "FROZEN=on")
# A root-declared MCP server has no executable-gate level (first-party, never parked).
_ROOT_MCP_CONTEXT = (*_COMMON, "GATE=none", "FROZEN=on")
# (op, form) -> the context ids its real steps must earn (``context_pass`` and the
# ordinary steps of the spine reach every one).
X_CONTEXT: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("install", "inline-mcp"): _ROOT_MCP_CONTEXT,
    ("install", "mcp-registry"): _ROOT_MCP_CONTEXT,
    ("install", "plugin-at-mkt"): _MANIFEST_CONTEXT,
    ("install", "native"): _MANIFEST_CONTEXT,
    ("install", "pkg-registry"): _MANIFEST_CONTEXT,
    # A local bundle has no manifest entry (``install --frozen`` never reads it) and
    # its install refuses ``--update`` (exit 2).
    ("install", "bundle"): (*(c for c in _COMMON if c != "UPDATE=on"), _UNAPPROVED),
    ("update", "marketplace-object"): ("DEPLOYED=clean",),
    ("update", "pkg-registry"): ("DEPLOYED=clean",),
}
_FLAG_CONTEXT = {
    "--force": "FORCE=on",
    "--update": "UPDATE=on",
    "--frozen": "FROZEN=on",
    "--target": "SELECT=on",
}


def x_cells() -> frozenset[str]:
    """Context obligation cells (see the module docstring)."""
    return frozenset(
        f"X|{op}|{form}|{context}"
        for (op, form), contexts in X_CONTEXT.items()
        for context in contexts
    )


def package_name(token: str) -> str | None:
    """The world package a preview or lock token names (``../p-child`` -> p-child)."""
    last = token.rstrip("/").rsplit("/", 1)[-1].split("#", 1)[0].split("@", 1)[0]
    return next((name for name in _NAMES if last.startswith(name)), None)


def preview_names(output: str) -> frozenset[str]:
    names = {
        package_name(match.group(1))
        for match in map(_PREVIEW_LINE.match, output.splitlines())
        if match
    }
    return frozenset(name for name in names if name)


def real_changes(before: Observation, after: Observation) -> tuple[frozenset[str], frozenset[str]]:
    """(changed-or-added, removed) package names between two observations."""
    rows_before, rows_after = _rows(before), _rows(after)
    changed = {name for name, row in rows_after.items() if rows_before.get(name) != row}
    removed = set(rows_before) - set(rows_after)
    difference = before.artifacts.snapshot("project").diff(after.artifacts.snapshot("project"))
    for path in difference.added | difference.removed | difference.changed:
        parts = path.split("/")
        if parts[:2] == [".agents", "skills"] and len(parts) > 2:
            name = package_name(parts[2])
            (removed if path in difference.removed and len(parts) == 3 else changed).add(name)
    for server in _mcp_servers(before) ^ _mcp_servers(after):
        changed.add(package_name(server))
    return frozenset(changed - {None}), frozenset(removed - {None})


def _rows(observation: Observation) -> dict[str, dict]:
    lock = observation.lock
    if lock is None:
        return {}
    return {
        name: {key: dependency.to_dict().get(key) for key in _ROW_CONTENT}
        for dependency in lock.get_package_dependencies()
        if (name := package_name(dependency.repo_url or dependency.name or ""))
    }


def _mcp_servers(observation: Observation) -> frozenset[str]:
    path = observation.deploy_root / ".mcp.json"
    if not path.is_file():
        return frozenset()
    return frozenset(json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {})


# Strict known cells this machine discharges; #4214 and #4572 are fixed (LSP-only
# packages survive uninstall; a git -> registry switch drops the stale .apm-pin).
KNOWN_CELLS: tuple[KnownCell, ...] = ()


class SourceMachine(MachineBase):
    """Reference model of dependency forms, checked against the real CLI at project scope."""

    known = KNOWN_CELLS

    def __init__(self, bench: Bench) -> None:
        super().__init__(bench, ModelState(), KINDS)
        self.slots: dict[str, Choice] = {}
        self.entries: dict[str, object] = {}
        self.shape = "github"
        self.network = "online"
        self.converged = False
        self.registered = False
        # Slot -> targets of the last resolving step that handled it: a later step under
        # the same targets is "unchanged" for that slot.
        self.resolved_targets: dict[str, frozenset[str]] = {}

    # -- model -----------------------------------------------------------------

    def declared_model(self) -> ModelState:
        names = {c.package for c in self.slots.values() if c.package in KINDS}
        names |= {"p-skill"} if "remote-skill" in self.slots else set()
        names |= {"p-exec"} if "remote-exec" in self.slots else set()
        if names & _DIAMOND:
            names.add("p-child")
        return ModelState(
            declared=frozenset(names), targets=self.model.targets, exec_gate=self.model.exec_gate
        )

    def remote(self) -> bool:
        return bool({"remote-skill", "remote-exec", "p-registry"} & set(self.slots))

    def exits(self, op: str, *, dry_run: bool = False) -> frozenset[int]:
        if op == "compile" and not self.bench.lock_path.is_file():
            return frozenset({1})  # "No APM content found to compile"
        if self.network == "offline" and self.remote() and op in ("install", "update", "lock"):
            return frozenset({0, 1})
        return frozenset({0})

    def cells(self, op: str, mode: str, choices: Collection[Choice]) -> list[str]:
        cells = []
        for choice in choices:
            if not applies(op, choice.form):
                continue
            cells.append(f"T5|{mode}|{op}|{choice.form}")
            cells += [f"T3|{op}|{choice.form}|{kind}" for kind in choice.prims]
        return cells

    def contexts(self, step: Step, op: str, args: tuple[str, ...], choice: Choice) -> list[str]:
        """The ``X`` ids of *choice* that this successful real step of *op* ran in."""
        found = {f"SCOPE={self.bench.scope}"}
        if self.network == "online":
            found.add("NET=online")
        if self.model.targets == self.resolved_targets.get(choice.slot, self.model.targets):
            shape = "single" if len(self.model.targets) == 1 else "multi"
            found.add(f"TGT={shape}-unchanged")
        found |= {context for flag, context in _FLAG_CONTEXT.items() if flag in args}
        # GATE=none is "no executables gate configured" (factors.py GateLevel "none");
        # with the gate on, only the executable kinds of a package have a gate level.
        if "executables" not in self.bench.manifest():
            found.add("GATE=none")
        elif choice.form not in _MCP_FORMS:
            # The project's own MCP servers are first-party: the gate never parks them.
            gated = choice.kinds - UNGATED_KINDS
            found |= {f"GATE={self.model.gate(choice.package, kind)}" for kind in gated}
        if any(cell.startswith(f"{choice.package}/") for cell in step.before.deployed):
            found.add("DEPLOYED=clean")
        listed = X_CONTEXT.get((op, choice.form), ())
        return [f"X|{op}|{choice.form}|{context}" for context in sorted(found & set(listed))]

    # -- manifest ----------------------------------------------------------------

    def entry(self, choice: Choice) -> object:
        packages = self.bench.world.packages
        if choice.form == "shorthand":
            return f"lifecycle/p-skill#{REF}"
        if choice.form == "git-object":
            return {
                "git": f"{self.bench.world.services.host_alias(self.shape)}/p-skill.git",
                "ref": REF,
            }
        if choice.form == "marketplace-object":
            return {"name": "p-skill", "marketplace": MARKET}
        if choice.form == "pkg-registry":
            return {
                "registry": registry.NAME,
                "id": f"lifecycle/{choice.package}",
                "version": "1.0.0",
            }
        return packages[choice.package].spec

    def selector(self, choice: Choice) -> str:
        """The ``apm uninstall`` argument that selects *choice*."""
        if choice.form in ("marketplace-object", "plugin-at-mkt"):
            return f"{choice.package.split('-bundle')[0]}@{MARKET}"
        if choice.form in ("shorthand", "pkg-registry"):
            return f"lifecycle/{choice.package}"
        entry = self.entries[choice.slot]
        return entry["git"] if isinstance(entry, dict) and "git" in entry else str(entry)

    def write_slot(self, slot: str, entry: object | None) -> None:
        data = self.bench.manifest()
        deps = data.setdefault("dependencies", {})
        if slot in _MCP_SERVERS:
            server = _MCP_SERVERS[slot]
            deps["mcp"] = [m for m in deps.get("mcp") or [] if server not in str(m)]
        else:
            old = self.entries.get(slot)
            deps["apm"] = [e for e in deps.get("apm") or [] if e != old]
            if entry is not None:
                deps["apm"].append(entry)
        self.bench.write_manifest(data)

    # -- steps -------------------------------------------------------------------

    def command(
        self,
        op: str,
        args: tuple[str, ...],
        choices: Collection[Choice],
        dry_run: bool,
        verify: Callable[[Step], None] | None = None,
    ) -> Step:
        """Run *op* (after its ``--dry-run`` preview when asked) and credit T3/T5 cells."""
        requested = self.declared_model()
        if op == "uninstall":
            requested = self._without(requested, choices)
        allowed = requested if op not in _KEEPING else self._keeping(requested)
        preview = self.preview(op, args, choices) if dry_run and op not in _NO_DRY_RUN else None
        converged = self.converged and self.network != "offline" and op != "uninstall"
        step = self.step(
            (op, *args),
            exits=self.exits(op),
            cells=self.cells(op, "real", choices),
            converged=converged,
            model=allowed,
            allowed={"home": (*_PROJECT_HOME_ALLOWED, *_CATALOG_CACHE, _PINS)},
        )
        self.egress(step)
        if step.result.returncode == 0:
            if verify is not None:
                verify(step)
            self.credit(step, op, args, choices, preview)
            self.model = requested
            self.converged = op not in _KEEPING or self.converged
            if op not in _KEEPING:
                for choice in choices:
                    self.resolved_targets[choice.slot] = self.model.targets
        return step

    def preview(
        self, op: str, args: tuple[str, ...], choices: Collection[Choice]
    ) -> tuple[Step, frozenset[str]] | None:
        step = self.step(
            (op, *args, "--dry-run"),
            exits=self.exits(op, dry_run=True),
            cells=self.cells(op, "dry-run", choices),
            read_only=True,
            mutating=False,
            allowed={"home": (*_PROJECT_HOME_ALLOWED, *_CATALOG_CACHE)},
        )
        self.egress(step)
        if step.result.returncode != 0:
            return None
        for cell in self.cells(op, "dry-run", choices):
            if cell.startswith("T5|"):
                check_obligation(
                    cell,
                    strength="e2e-evidence",
                    before=step.before,
                    after=step.after,
                    result=step.result,
                    evidence=Evidence(unchanged=_DURABLE, exit=0),
                )
        output = f"{step.result.stdout}\n{step.result.stderr}"
        incomplete = frozenset({"*incomplete"}) if _INCOMPLETE.search(output) else frozenset()
        return step, preview_names(output) | incomplete

    def credit(
        self, step: Step, op: str, args: tuple[str, ...], choices: Collection[Choice], preview
    ) -> None:
        observed = expected = True
        if preview is not None:
            changed, removed = real_changes(preview[0].before, step.after)
            named = preview[1]
            unnamed = changed - named
            if "*incomplete" not in named:
                unnamed |= removed - named
            observed, expected = sorted(unnamed), []
        cells = self.cells(op, "real", choices)
        for choice in choices:
            if applies(op, choice.form):
                cells += self.contexts(step, op, args, choice)
        for cell in dict.fromkeys(cells):
            if cell in KNOWN_CELLS_BY_ID:
                continue
            check_obligation(
                cell,
                strength="e2e-evidence",
                before=step.before,
                after=step.after,
                result=step.result,
                evidence=Evidence(
                    unchanged={"outside": ("*",)},
                    exit=0,
                    expected=expected,
                    observed=observed,
                ),
            )

    def egress(self, step: Step) -> None:
        """Proxy-only law: online, every request reaches a world host through the proxy."""
        log = self.bench.world.services.egress_log()
        self.bench.world.services.reset()
        if self.network == "offline":
            assert not log, f"offline step reached the proxy: {log}"
            return
        strays = [
            entry for entry in log if entry["host"] not in WORLD_HOSTS or entry["status"] == 502
        ]
        assert not strays, f"{step.op.name} reached hosts outside the world: {strays}"

    def _without(self, model: ModelState, choices: Collection[Choice]) -> ModelState:
        slots = {c.slot for c in choices}
        kept = {k: v for k, v in self.slots.items() if k not in slots}
        saved, self.slots = self.slots, kept
        try:
            return self.declared_model()
        finally:
            self.slots = saved

    def _keeping(self, model: ModelState) -> ModelState:
        return model.declare(*self.model.declared)

    def _choices(self) -> list[Choice]:
        return list(self.slots.values())

    # -- rules (at most ten) -----------------------------------------------------

    @initialize()
    def register_marketplace(self) -> None:
        """``apm marketplace add``: register, repeat (I2), and refuse an unknown catalog (I7)."""
        url = self.bench.world.services.marketplace_url
        for converged in (False, True):
            self.egress(self.marketplace_add(url, {0}, converged))
        self.egress(self.marketplace_add(url.rsplit("/", 1)[0] + "/no-such-catalog", {1}, False))
        self.registered = True

    def marketplace_add(self, url: str, exits: set[int], converged: bool) -> Step:
        return self.step(
            ("marketplace", "add", url),
            exits=exits,
            converged=converged,
            mutating=True,
            forbidden={"project": ("*",), "outside": ("*",)},
            allowed={},
        )

    @rule(choice=st.sampled_from(CHOICES), dry_run=st.booleans())
    def declare(self, choice: Choice, dry_run: bool) -> None:
        if choice.form in _CLI_FORMS:
            self.declare_by_cli(choice, dry_run)
            return
        entry = self.entry(choice)

        def change() -> None:
            self.write_slot(choice.slot, entry)

        self.edit(f"declare-{choice.form}", change)
        self.entries[choice.slot] = entry
        self.slots[choice.slot] = choice
        self.converged = False

    def declare_by_cli(self, choice: Choice, dry_run: bool, flags: tuple[str, ...] = ()) -> None:
        if choice.form == "mcp-registry":
            args: tuple[str, ...] = ("--mcp", REGISTRY_SERVER)
        elif choice.form == "inline-mcp":
            args = ("--mcp", INLINE_SERVER, "--", INERT_COMMAND, "--stdio")
        elif choice.form == "bundle":
            source = choice.package.removesuffix("-bundle")
            args = (self.bench.world.packages[f"{source}:bundle"].spec, *flags)
        else:
            args = (f"p-exec@{MARKET}",)
        had = self.slots.get(choice.slot)
        if had is not None and had.form != choice.form:
            self.undeclare(choice.slot)  # one identity, one declaring form
            had = None
        before = self._apm_entries()
        self.slots[choice.slot] = choice
        self.converged = False
        step = self.command("install", args, (choice,), dry_run and choice.form not in _MCP_FORMS)
        if step.result.returncode != 0:
            if had is None:
                self.slots.pop(choice.slot)
            return
        added = [entry for entry in self._apm_entries() if entry not in before]
        if choice.form == "plugin-at-mkt" and added:
            self.entries[choice.slot] = added[0]

    def _apm_entries(self) -> list:
        return list(self.bench.manifest().get("dependencies", {}).get("apm") or [])

    @rule(slot=st.sampled_from(sorted({c.slot for c in CHOICES})))
    def undeclare(self, slot: str) -> None:
        if slot not in self.slots:
            return
        self.edit(f"undeclare-{self.slots[slot].form}", lambda: self.write_slot(slot, None))
        self.slots.pop(slot)
        self.entries.pop(slot, None)
        self.converged = False

    @rule(
        op=st.sampled_from(("install", "update", "prune", "compile", "lock")), dry_run=st.booleans()
    )
    def reconcile(self, op: str, dry_run: bool) -> None:
        args = ("--yes",) if op == "update" else ()
        choices = [c for c in self._choices() if c.form != "bundle"]
        self.command(op, args, choices, dry_run)

    def gate_off_pass(self) -> None:
        """No executables gate: every declared form deploys without approval (GATE=none).

        Restoring the gate then retires every package executable that is no longer
        approved, while the project's own MCP servers (declared in its apm.yml, so
        first-party) stay configured.
        """
        saved = self.bench.manifest()["executables"]

        def set_gate(value: object) -> Callable[[], None]:
            def change() -> None:
                data = self.bench.manifest()
                data.pop("executables", None)
                if value is not None:
                    data["executables"] = value
                self.bench.write_manifest(data)

            return change

        self.edit("executables-gate-off", set_gate(None))
        self.model = self.model.with_exec_gate(False)
        self.converged = False
        manifest = [c for c in self._choices() if c.form != "bundle"]
        self.command("install", (), manifest, False, verify=self.gate_off_deploys_everything)
        self.edit("executables-gate-on", set_gate(saved))
        self.model = self.model.with_exec_gate(True)
        self.converged = False
        self.command("install", (), manifest, False, verify=self.gate_on_keeps_first_party_mcp)

    def deploys_what_the_model_allows(self, step: Step) -> None:
        """A resolving step deploys exactly the observable surfaces the model allows."""
        wanted = {
            "/".join(cell)
            for cell in expected_deployed(self.declared_model(), self.kinds)
            if (cell[1], cell[2]) in _SURFACE_GLOBS
        }
        deployed = set(step.after.deployed)
        assert deployed == wanted, f"deployed {sorted(deployed)}, model {sorted(wanted)}"

    def root_mcp_problems(self, step: Step) -> list[str]:
        """The project's declared MCP servers that are not configured or not locked."""
        lock = step.after.lock
        places = {
            "configured in .mcp.json": set(_mcp_servers(step.after)),
            "recorded in the lock": set(lock.mcp_servers if lock else ()),
        }
        problems = []
        for choice in self._choices():
            if choice.form in _MCP_FORMS:
                names = _MCP_NAMES[choice.package]
                problems += [
                    f"{choice.package} not {place}"
                    for place, found in places.items()
                    if not names & found
                ]
        return problems

    def gate_off_deploys_everything(self, step: Step) -> None:
        """No gate: every allowed surface is deployed and no MCP server is parked."""
        self.deploys_what_the_model_allows(step)
        problems = self.root_mcp_problems(step)
        assert not problems, "; ".join(problems)
        output = f"{step.result.stdout}\n{step.result.stderr}"
        assert "Parked MCP server" not in output, output

    def gate_on_keeps_first_party_mcp(self, step: Step) -> None:
        """Gate restored: package executables retire; the project's own MCP servers stay."""
        self.deploys_what_the_model_allows(step)
        problems = self.root_mcp_problems(step)
        assert not problems, "; ".join(problems)

    def context_pass(self) -> None:
        """Install every declared form under --force/--update/--target and --frozen, multi-target."""
        self.retarget(TARGET_SETS[1])
        self.reconcile("install", False)
        selected = ("--target", ",".join(sorted(self.model.targets)))
        manifest = [c for c in self._choices() if c.form != "bundle"]
        self.command("install", ("--force", "--update", *selected), manifest, False)
        self.command("install", ("--frozen",), manifest, False)
        bundle = next((c for c in self._choices() if c.form == "bundle"), None)
        if bundle is not None:
            # The first install under the new targets widens them; the second is unchanged.
            self.declare_by_cli(bundle, False)
            self.declare_by_cli(bundle, False, ("--force", *selected))
        self.retarget(TARGET_SETS[0])
        self.reconcile("install", False)

    @rule(slot=st.sampled_from(sorted({c.slot for c in CHOICES})), dry_run=st.booleans())
    def uninstall(self, slot: str, dry_run: bool) -> None:
        choice = self.slots.get(slot)
        if choice is None or not applies("uninstall", choice.form):
            return
        self.command("uninstall", (self.selector(choice),), (choice,), dry_run)
        if self.entries.get(slot) not in self._apm_entries():
            self.slots.pop(slot, None)
            self.entries.pop(slot, None)

    @rule(mode=st.sampled_from(NETWORK))
    def network_mode(self, mode: str) -> None:
        env = self.bench.world.env
        proxy = self.bench.world.services.proxy_url

        def change() -> None:
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY"):
                env.pop(name, None)
            env["HTTPS_PROXY"] = env["https_proxy"] = _CLOSED_PROXY if mode == "offline" else proxy
            env["NO_PROXY"] = env["no_proxy"] = "" if mode == "proxy-only" else "localhost"
            if mode == "proxy-only":
                env["HTTP_PROXY"] = env["ALL_PROXY"] = proxy

        self.edit(f"network-{mode}", change)
        self.network = mode
        self.bench.world.services.reset()

    @rule(chosen=st.sampled_from(TARGET_SETS))
    def retarget(self, chosen: tuple[str, ...]) -> None:
        targets = chosen

        def change() -> None:
            data = self.bench.manifest()
            data["targets"] = list(targets)
            self.bench.write_manifest(data)

        self.edit("retarget-" + "-".join(targets), change)
        self.model = self.model.with_targets(*targets)
        self.converged = False

    @rule(shape=st.sampled_from(SHAPES))
    def host_alias(self, shape: str) -> None:
        self.shape = shape
        self.declare(_BY_FORM["git-object"], False)


_DURABLE = {
    "home": (_PINS,),
    "project": (
        "apm.yml",
        "apm.lock.yaml",
        "apm_modules",
        ".agents",
        ".github",
        ".claude",
        ".mcp.json",
    ),
    "outside": ("*",),
}


def replay_spine(machine: SourceMachine) -> None:
    """Visit every T3 and T5 cell: each form through each op, with and without --dry-run."""
    machine.register_marketplace()
    for choice in CHOICES:
        if choice.form in ("local-path", "native"):
            machine.declare(choice, False)
    for form in ("plugin-at-mkt", "bundle", "mcp-registry", "inline-mcp"):
        for choice in CHOICES:
            if choice.form == form:
                machine.declare(choice, True)
    remote_skills = (*(_BY_FORM[f] for f in ("shorthand", "git-object", "marketplace-object")),)
    remote_skills += (_REGISTRY_SKILL,)
    for choice in remote_skills:
        machine.declare(choice, False)
        for op in ("install", "update", "prune", "compile", "lock"):
            machine.reconcile(op, True)
        if choice is _REGISTRY_SKILL:
            machine.context_pass()
            machine.gate_off_pass()
    # p-lsp still survives this uninstall (#4214).
    machine.uninstall("remote-skill", False)
    for slot in ("remote-exec", "p-lsp"):
        machine.uninstall(slot, True)
    machine.declare(_REGISTRY_EXEC, False)
    for op in ("install", "update", "prune", "compile", "lock"):
        machine.reconcile(op, True)
    machine.uninstall("remote-exec", True)
    for choice in remote_skills:
        machine.declare(choice, False)
        machine.reconcile("install", False)
        machine.uninstall("remote-skill", True)
    for slot in ("p-parent", "p-canvas", "p-native-claude", "p-native-claude-bin"):
        machine.uninstall(slot, True)
    for slot in _MCP_SERVERS:
        machine.undeclare(slot)
    machine.reconcile("prune", False)
    for shape in HOST_ALIASES:
        machine.host_alias(shape)
        machine.reconcile("install", False)
    for targets in (*TARGET_SETS[1:], TARGET_SETS[0]):
        machine.retarget(targets)
        machine.reconcile("install", False)
    for mode in NETWORK[1:] + NETWORK[:1]:
        machine.network_mode(mode)
        machine.reconcile("install", False)


def test_source_spine_covers_every_form_and_op(lifecycle_template, tmp_path: Path) -> None:
    """Every OP x SRC x PRIM and DRYRUN x OP x SRC cell earns an oracle receipt."""
    machine = SourceMachine(Bench(WorldPool(lifecycle_template, tmp_path).fresh(), "project"))
    replay_spine(machine)
    strict = {cell.label for cell in machine.known}
    assert machine.expected_seen == strict, sorted(strict - machine.expected_seen)
    earned = earned_cells(current_node(), "T") | earned_cells(current_node(), "X|")
    missing = (t3_cells() | t5_cells() | x_cells()) - earned - set(KNOWN_CELLS_BY_ID)
    assert not missing, f"T3/T5/X cells without an oracle receipt: {sorted(missing)}"


def _cache_bytes(bench: Bench) -> dict[str, bytes]:
    """Every byte under apm_modules plus the lockfile: the I2 fixpoint surface."""
    modules = bench.manifest_dir / "apm_modules"
    files = {
        path.relative_to(bench.manifest_dir).as_posix(): path.read_bytes()
        for path in sorted(modules.rglob("*"))
        if path.is_file()
    }
    files[bench.lock_path.name] = bench.lock_path.read_bytes()
    return files


def test_git_to_registry_switch_drops_the_stale_cache_pin(
    lifecycle_template, tmp_path: Path
) -> None:
    """X|install|pkg-registry|UPDATE=on (law I2, #4572).

    PROMISE: P-INV-I2 -- a converged project is a byte fixpoint: after a git ->
    registry source switch the first install drops the git ``.apm-pin`` (owner:
    src/apm_cli/install/cache_pin.py), so a repeat install and ``install --update``
    change no bytes. Control: a registry -> git switch writes a fresh marker.
    """
    from apm_cli.install.cache_pin import MARKER_FILENAME, verify_marker

    bench = Bench(WorldPool(lifecycle_template, tmp_path).fresh(), "project")
    machine = SourceMachine(bench)
    pin = bench.manifest_dir / "apm_modules" / "lifecycle" / "p-skill" / MARKER_FILENAME

    def locked_commit() -> str | None:
        rows = machine.observe().lock.dependencies
        return next(r.resolved_commit for r in rows.values() if "p-skill" in r.repo_url)

    machine.declare(_BY_FORM["shorthand"], False)
    machine.reconcile("install", False)
    commit = locked_commit()
    assert pin.is_file() and commit, "a git install writes the cache pin"
    verify_marker(pin.parent, commit)

    machine.declare(_REGISTRY_SKILL, False)
    machine.reconcile("install", False)
    assert locked_commit() is None
    assert not pin.exists(), "the first registry install must drop the stale git .apm-pin"

    converged = _cache_bytes(bench)
    for args in ((), ("--update",)):
        machine.command("install", args, [_REGISTRY_SKILL], False)
        assert _cache_bytes(bench) == converged, f"install {args} changed bytes (I2)"
    audit = bench.run(("audit",))
    assert audit.returncode == 0, f"{audit.stdout}\n{audit.stderr}"

    machine.declare(_BY_FORM["shorthand"], False)
    machine.reconcile("install", False)
    commit = locked_commit()
    assert pin.is_file() and commit, "a registry -> git switch writes a fresh cache pin"
    verify_marker(pin.parent, commit)


def test_source_generated_sequences_keep_cross_cutting_laws(
    lifecycle_template, tmp_path: Path
) -> None:
    """Generate and shrink form x op sequences, one fresh world per example."""
    pool = WorldPool(lifecycle_template, tmp_path)
    run_state_machine_as_test(
        lambda: SourceMachine(Bench(pool.fresh(), "project")),
        settings=machine_settings(pr_examples=4, nightly_examples=25, steps=8),
    )


# T3/T5 cells whose oracle fails on main -> tracking issue (a strict known cell discharges them).
KNOWN_CELLS_BY_ID: dict[str, str] = {}
