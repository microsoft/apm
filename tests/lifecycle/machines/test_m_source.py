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
Claude plugin, MCP registry server. ``apm lock`` has no ``--dry-run`` flag, so
its T5 cells are ``real`` only; the local bundle is a side door with no
manifest entry, so only ``install`` applies to it; an MCP registry server is
removed by editing ``apm.yml`` (``apm uninstall`` selects APM packages only).

A dry-run step must leave every durable byte unchanged (I5 read-only); the real
command then runs and every package whose lock row, deployed skill or MCP entry
it changed must be named in the preview. The preview lists every locally known
dependency, so the comparison is ``real <= preview``; when the preview declares
itself incomplete ("Orphan removals are not previewed") removed rows are exempt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
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
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, Observation, check_obligation
from tests.lifecycle.receipts import current_node
from tests.lifecycle.rows import _PROJECT_HOME_ALLOWED
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
    "registry",
)
SHAPES = ("github", *HOST_ALIASES)
TARGET_SETS = (("copilot",), ("copilot", "claude"), ("claude",))
NETWORK = ("online", "offline", "proxy-only")
REF = "v1.0.0"
MARKET = MARKETPLACE_NAME
WORLD_HOSTS = frozenset(
    {GITHUB_HOST, API_HOST, RAW_HOST, REGISTRY_HOST, *(host for host, _ in HOST_ALIASES.values())}
)
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
    """One dependency form and the world package it declares."""

    form: str
    package: str
    kinds: frozenset[str]

    @property
    def slot(self) -> str:
        """Forms of one identity share a slot: declaring one replaces the other."""
        if self.package == "p-skill":
            return "remote-skill"
        return self.package if self.form != "plugin-at-mkt" else "remote-exec"


_SKILL = frozenset({"skill"})
_EXEC = frozenset({"hook", "bin", "mcp"})
CHOICES = (
    Choice("shorthand", "p-skill", _SKILL),
    Choice("git-object", "p-skill", _SKILL),
    Choice("marketplace-object", "p-skill", _SKILL),
    Choice("plugin-at-mkt", "p-exec", _EXEC),
    Choice("bundle", "p-exec-bundle", _EXEC),
    Choice("local-path", "p-parent", _SKILL),
    Choice("local-path", "p-parent2", _SKILL),
    Choice("local-path", "p-lsp", frozenset({"lsp"})),
    Choice("local-path", "p-canvas", frozenset({"canvas"})),
    Choice("native", "p-native-claude", frozenset({"native"})),
    Choice("registry", "p-registry", frozenset({"mcp"})),
)
_BY_FORM = {choice.form: choice for choice in CHOICES}
_DIAMOND = frozenset({"p-parent", "p-parent2"})
# Observed packages and their kinds (registry and bundle deploy no observed surface).
KINDS: Mapping[str, frozenset[str]] = {
    "p-skill": _SKILL,
    "p-exec": _EXEC,
    "p-child": _SKILL,
    **{c.package: c.kinds for c in CHOICES if c.form in ("local-path", "native")},
}
_NAMES = sorted({*KINDS, "p-registry"}, key=len, reverse=True)


def applies(op: str, form: str) -> bool:
    """Whether *op* acts on a dependency of *form* (see the module docstring)."""
    if form == "bundle":
        return op == "install"
    return not (form == "registry" and op == "uninstall")


def t3_cells() -> frozenset[str]:
    """OP x SRC x PRIM obligation cells."""
    return frozenset(
        f"T3|{op}|{choice.form}|{kind}"
        for op in OPS
        for choice in CHOICES
        if applies(op, choice.form)
        for kind in choice.kinds
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


def _pins_written(before: Observation, after: Observation) -> bool:
    difference = before.artifacts.snapshot("home").diff(after.artifacts.snapshot("home"))
    return _PINS in difference.added | difference.changed


def _dry_run_pins(machine, op, before, after, result) -> bool:
    del machine
    return result is not None and "DRYRUN" in op.modifiers and _pins_written(before, after)


def _gated_rows(observation: Observation) -> bool:
    lock = observation.lock
    return lock is not None and any(
        dependency.to_dict().get("exec_status") == "gated_pending_approval"
        for dependency in lock.get_package_dependencies()
    )


def _lock_drops_exec_status(machine, op, before, after, result) -> bool:
    del after
    return result is not None and op.name == "lock" and machine.converged and _gated_rows(before)


def _lsp_survivor(machine, op, before=None, after=None, result=None) -> bool:
    """#4214: uninstall with a surviving LSP-only local package fails its cleanup."""
    del before, after
    return (
        result is not None
        and result.returncode == 1
        and op.name == "uninstall"
        and "DRYRUN" not in op.modifiers
        and machine.lsp_survives
    )


def _skill_dirs(observation: Observation) -> int:
    paths = observation.artifacts.snapshot("project").paths
    return sum(1 for path in paths if path.count("/") == 2 and path.startswith(".agents/skills/"))


def _unattributed_rebuild(machine, op, before, after, result) -> bool:
    """#4219: uninstall repeats one unattributed integration pair per surviving package."""
    del machine, before
    return (
        result is not None
        and op.name == "uninstall"
        and "DRYRUN" not in op.modifiers
        and result.returncode == 0
        and _skill_dirs(after) >= 2
    )


KNOWN_CELLS = (
    KnownCell(
        "uninstall-unattributed-rebuild",
        "I9",
        "#4219",
        r"^duplicate 'integrated' line(: .*)?$",
        _unattributed_rebuild,
    ),
    KnownCell(
        "uninstall-lsp-survivor",
        "I7",
        "#4214",
        r"^failed uninstall (changed (the manifest|the lockfile|deployed surfaces|durable files: .*)"
        r"|exited 1 without announcing why)$",
        _lsp_survivor,
    ),
    KnownCell(
        "lock-drops-exec-status",
        "I2",
        "#4213",
        r"^lock or manifest semantics changed on a converged state$",
        _lock_drops_exec_status,
    ),
    KnownCell(
        "dry-run-writes-ref-pin",
        "I5",
        "#4212",
        r"wrote home:\['\.apm/cache/marketplace/version-pins\.json'\]$",
        _dry_run_pins,
    ),
)


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
        self.lsp_survives = False

    # -- model -----------------------------------------------------------------

    def declared_model(self) -> ModelState:
        names = {c.package for c in self.slots.values() if c.package in KINDS}
        names |= {"p-skill"} if "remote-skill" in self.slots else set()
        names |= {"p-exec"} if "remote-exec" in self.slots else set()
        if names & _DIAMOND:
            names.add("p-child")
        return ModelState(declared=frozenset(names), targets=self.model.targets)

    def remote(self) -> bool:
        return bool({"remote-skill", "remote-exec", "p-registry"} & set(self.slots))

    def exits(self, op: str, *, dry_run: bool = False) -> frozenset[int]:
        if op == "uninstall" and self.lsp_survives and not dry_run:
            return frozenset({0, 1})  # strict known cell #4214 owns the exit 1
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
            cells += [f"T3|{op}|{choice.form}|{kind}" for kind in choice.kinds]
        return cells

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
        return packages[choice.package].spec

    def selector(self, choice: Choice) -> str:
        """The ``apm uninstall`` argument that selects *choice*."""
        if choice.form in ("marketplace-object", "plugin-at-mkt"):
            return f"{choice.package.split('-bundle')[0]}@{MARKET}"
        if choice.form == "shorthand":
            return "lifecycle/p-skill"
        entry = self.entries[choice.slot]
        return entry["git"] if isinstance(entry, dict) and "git" in entry else str(entry)

    def write_slot(self, slot: str, entry: object | None) -> None:
        data = self.bench.manifest()
        deps = data.setdefault("dependencies", {})
        if slot == "p-registry":
            deps["mcp"] = [m for m in deps.get("mcp") or [] if REGISTRY_SERVER not in str(m)]
        else:
            old = self.entries.get(slot)
            deps["apm"] = [e for e in deps.get("apm") or [] if e != old]
            if entry is not None:
                deps["apm"].append(entry)
        self.bench.write_manifest(data)

    # -- steps -------------------------------------------------------------------

    def command(
        self, op: str, args: tuple[str, ...], choices: Collection[Choice], dry_run: bool
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
            self.credit(step, op, choices, preview)
            self.model = requested
            self.converged = op not in _KEEPING or self.converged
            if op == "lock" and _gated_rows(step.before):
                self.converged = False  # #4213: the rewritten lock is not install-converged
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
        pinned = _pins_written(step.before, step.after)
        for cell in self.cells(op, "dry-run", choices):
            if pinned and ("marketplace" in cell or "plugin-at-mkt" in cell):
                continue  # strict known cell #4212: this preview wrote the ref pin
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

    def credit(self, step: Step, op: str, choices: Collection[Choice], preview) -> None:
        observed = expected = True
        if preview is not None:
            changed, removed = real_changes(preview[0].before, step.after)
            named = preview[1]
            unnamed = changed - named
            if "*incomplete" not in named:
                unnamed |= removed - named
            observed, expected = sorted(unnamed), []
        for cell in self.cells(op, "real", choices):
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

    def installed_lsp(self) -> bool:
        lock = self.bench.lock_path
        return lock.is_file() and "_local/p-lsp" in lock.read_text(encoding="utf-8")

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
        if choice.form in ("plugin-at-mkt", "bundle", "registry"):
            self.declare_by_cli(choice, dry_run)
            return
        entry = self.entry(choice)

        def change() -> None:
            self.write_slot(choice.slot, entry)

        self.edit(f"declare-{choice.form}", change)
        self.entries[choice.slot] = entry
        self.slots[choice.slot] = choice
        self.converged = False

    def declare_by_cli(self, choice: Choice, dry_run: bool) -> None:
        if choice.form == "registry":
            args: tuple[str, ...] = ("--mcp", REGISTRY_SERVER)
        elif choice.form == "bundle":
            args = (self.bench.world.packages["p-exec:bundle"].spec,)
        else:
            args = (f"p-exec@{MARKET}",)
        before = self._apm_entries()
        had = self.slots.get(choice.slot)
        self.slots[choice.slot] = choice
        self.converged = False
        step = self.command("install", args, (choice,), dry_run and choice.form != "registry")
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

    @rule(slot=st.sampled_from(sorted({c.slot for c in CHOICES})), dry_run=st.booleans())
    def uninstall(self, slot: str, dry_run: bool) -> None:
        choice = self.slots.get(slot)
        if choice is None or not applies("uninstall", choice.form):
            return
        self.lsp_survives = "p-lsp" in self.slots and slot != "p-lsp" and self.installed_lsp()
        step = self.command("uninstall", (self.selector(choice),), (choice,), dry_run)
        if self.lsp_survives and step.result.returncode == 1:
            self.lsp_survives = False
            self.converged = False
            self.model = self._without(self.model, (choice,))
            self.slots.pop(slot, None)
            self.entries.pop(slot, None)
            # Execute the printed hint: "Run 'apm install' to resync remaining integrations."
            assert "Run 'apm install' to resync" in step.result.stdout, step.result.stdout
            self.reconcile("install", False)
            return
        self.lsp_survives = False
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
    for form in ("plugin-at-mkt", "bundle", "registry"):
        machine.declare(_BY_FORM[form], True)
    remote_forms = ("shorthand", "git-object", "marketplace-object")
    for form in remote_forms:
        machine.declare(_BY_FORM[form], False)
        for op in ("install", "update", "prune", "compile", "lock"):
            machine.reconcile(op, True)
    # p-lsp still survives: the strict known cell #4214 fails this uninstall.
    machine.uninstall("remote-skill", False)
    for slot in ("remote-exec", "p-lsp"):
        machine.uninstall(slot, True)
    for form in remote_forms:
        machine.declare(_BY_FORM[form], False)
        machine.reconcile("install", False)
        machine.uninstall("remote-skill", True)
    for slot in ("p-parent", "p-canvas", "p-native-claude"):
        machine.uninstall(slot, True)
    machine.undeclare("p-registry")
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
    earned = earned_cells(current_node(), "T")
    missing = (t3_cells() | t5_cells()) - earned - set(KNOWN_CELLS_BY_ID)
    assert not missing, f"T3/T5 cells without an oracle receipt: {sorted(missing)}"


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
