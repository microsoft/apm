"""M-GATE plugin bin spine: a whole Claude plugin's bin/ at project scope (#4894).

PROMISE: P-INV-I4 -- a deny, revoked approval or stricter policy never widens
what is deployed; the next resolving command retires bin/ the gate no longer
allows (source: tests/lifecycle/oracles.py).
PROMISE: P-OWN-executable-item-admission -- a native plugin under
.agents/plugins that the current approvals or policy no longer allow is
retired by ``apm prune`` as one unit: its root, bin/ included, and its
declarations in the harness settings files go (source:
docs/src/content/docs/reference/cli/prune.md, src/apm_cli/native_plugins/
uninstall.py retire_gated_native_plugins).
CELL: OP x PRIM=bin x GATE (T1) at project scope.

Loose bin/ deploys only at user scope (install/exec_gate.py
plugin_bin_deployable), but a trusted Claude plugin that ships bin/ deploys
whole to ``.agents/plugins/<plugin>/`` at project scope
(claude_plugins/admission.py), so prune and marketplace reach bin/ there.
The spine drives ``p-native-claude-bin`` from git on the claude target
through three gate levels: ``none`` (no executables block), then
``unapproved-exec`` (an empty block turns the gate on over the deployed
plugin) and ``denied-dependency`` (org policy denies the plugin). Every
deploying install passes ``--trust-bin`` so the gate alone decides bin/.
Each prune that retires the plugin must remove every file of its root and
its ``enabledPlugins`` keys and drop its lockfile record; every step runs
I1-I9. At ``allowed`` the deployed plugin is re-installed with each modifier
(``--frozen``, ``--update``, ``--force``, ``--dry-run``, ``--target``); each keeps
bin/ byte-identical, so it proves PRIM=bin with that modifier (``bin_context``).
``plugin_bin_cells`` labels the same script statically.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace

import yaml

from tests.lifecycle import driver
from tests.lifecycle.machines._gate_matrix import _mcp_servers, mcp_install_problems
from tests.lifecycle.machines._kit import Bench, KnownCell, Step, StepRunner
from tests.lifecycle.model.expected import expected_deployed
from tests.lifecycle.model.state import ModelState
from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import REGISTRY_SERVER
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner

PLUGIN = "p-native-claude-bin"
KINDS = {PLUGIN: frozenset({"bin"})}
TARGETS = frozenset({"claude"})
ROOT = f".agents/plugins/{PLUGIN}"
BIN_PATH = f"{ROOT}/bin/{PLUGIN}"
DENY_POLICY = "deny-native"
# Harness settings files whose enabledPlugins declare a project-scope plugin.
SETTINGS = (".claude/settings.json", ".github/copilot/settings.json")
_RETIRED = r"1 native plugin\(s\) removed"
# Exit codes pinned from main's behaviour (#4095 lane T-1b).
AUDIT_GATED = (1,)
# audit --ci names the decision the gate no longer makes and the command that records it.
_STALE_DECISION = r"exec-status-current details:"
AUDIT_DENIED = (1,)
# audit --ci names the org denylist entry that denies the plugin.
_DENYLISTED = r"dependency-denylist details:"
COMPILE_DENIED = (1,)
_COMPILE_BLOCKED = r"Compile blocked by org policy"
LOCK_DENIED = (1,)
_RESOLVE_BLOCKED = r"Blocked by org policy at .*\(dependency-denylist\)\. Nothing was changed"
# Strict known cells: the step must still fail its oracle on main; a pass is an error.
# approve under an org dependency deny says "approved" and points at apm install,
# which refuses: the approval names no org block (#5236).
_ORG_BLOCK_NAMED = r"(?i)org policy"
# The tracked misleading approval itself: it succeeds, announces the approval and
# points at an install that the org policy will refuse.
_APPROVED = rf"\[\+\] Approved \S*{PLUGIN} "
_INSTALL_HINT = r"Run 'apm install' to deploy the approved executables"
_PACK_BLOCKED = r"Pack blocked by org policy: \S*p-native-claude-bin\. Nothing was packed"
KNOWN_PLUGIN: dict[str, str] = {"G1|approve|bin|denied-dependency|project": "#5236"}
# The same cells for the known-cell issue snapshot (tests/quality/known_cell_issues.py).
KNOWN_CELLS = (
    KnownCell(
        "G1|approve|bin|denied-dependency|project",
        "I7",
        "#5236",
        _ORG_BLOCK_NAMED,
        lambda *_args: False,
    ),
)
# The project gate matrix (and the pack spine, for pack) already label their steps under
# the org deny with these ops (their bin/ never deploys, so they credit no bin cell,
# #4981); this spine's steps carry a ``plugin`` suffix there so each machine id keeps one
# witness.
_MATRIX_DENIED_OPS = frozenset(
    {"audit-ci", "compile", "install", "install-frozen", "lock", "mcp-install", "pack", "update"}
)


class PluginBinBench(Bench):
    """A project bench that also observes the plugin's bin/ as the claude bin surface."""

    def surface_globs(self) -> Mapping[tuple[str, str], str | tuple[str, str]]:
        return {**super().surface_globs(), ("bin", "claude"): ".agents/plugins/{pkg}/bin/{pkg}"}


@dataclass
class PluginBinState:
    """The user's inputs for the plugin, as the spine has set them."""

    declared: bool = False
    gate_on: bool = False
    decision: str | None = None
    revoked: bool = False
    deployed: bool = False
    denied_by_policy: bool = False
    targets: frozenset[str] = TARGETS

    def gate(self) -> str:
        if self.denied_by_policy:
            return "denied-dependency"
        if not self.gate_on:
            return "none"
        if self.revoked:
            return "revoked-after-deploy"
        if self.decision == "allowed":
            return "allowed"
        return "denied-executable" if self.decision == "denied" else "unapproved-exec"

    def allows(self) -> bool:
        return self.gate() in ("none", "allowed")

    def model(self) -> ModelState:
        model = ModelState(
            declared=frozenset({PLUGIN}) if self.declared else frozenset(),
            decisions={(PLUGIN, "bin"): self.decision} if self.decision else {},
            revoked=frozenset({(PLUGIN, "bin")}) if self.revoked else frozenset(),
            targets=self.targets,
            exec_gate=self.gate_on,
        )
        return model.with_policy(DENY_POLICY, [PLUGIN]) if self.denied_by_policy else model


class GatePluginBin(StepRunner):
    """Run (or, when *bench* is None, only label) the plugin bin spine at project scope."""

    def __init__(self, bench: Bench | None) -> None:
        super().__init__(bench, ModelState(targets=TARGETS), KINDS)
        self.dry = bench is None
        self.state = PluginBinState()
        self.credited: set[str] = set()
        # Every file of the deployed plugin root, bin/ included (set by a deploy).
        self.root_files: frozenset[str] = frozenset()
        self._mcp_before: frozenset[str] | None = None

    def run(
        self, op: str, *words: str, retires: bool = False, exits=(0,), says: tuple[str, ...] = ()
    ) -> None:
        """Run one command, check I1-I9 and its outcome, credit its id, then advance."""
        gate = self.state.gate()
        cell = f"G1|{op}|bin|{gate}|project"
        if gate == "denied-dependency" and op in _MATRIX_DENIED_OPS:
            cell += "|plugin"
        self.credited.add(cell)
        requested = self._requested(op, retires)
        self._mcp_before = (
            _mcp_servers(self.bench.deploy_root) if not self.dry and op == "mcp-install" else None
        )
        if not self.dry and cell in KNOWN_PLUGIN:
            self._known(cell, words, requested)
        elif not self.dry:
            # A decision is recorded, not applied: the next resolving command retires
            # what it no longer allows (the deny prints that hint), as in the user spine.
            # install --mcp touches MCP configuration only and keeps bin/ byte-identical
            # at every gate level (U7), so it is not the command that retires it.
            decision = op in ("approve", "deny", "mcp-install")
            fields: dict[str, object] = {
                "mutating": op not in ("marketplace", "audit-ci", "pack") and not decision
            }
            if op in ("audit-ci", "pack"):
                fields["read_only"] = True
            if op == "marketplace":
                fields.update(forbidden={"project": ("*",), "outside": ("*",)}, allowed={})
            step = self.step(
                words, exits=frozenset(exits), cells=(cell,), model=requested.model(), **fields
            )
            self._check_outcome(op, requested, step, retires)
            self._credit(op, cell, step, retires, frozenset(exits), says)
        self.state = requested

    def _known(self, cell: str, words: tuple[str, ...], requested: PluginBinState) -> None:
        """Run a strict known cell: I1-I9 hold, but its own oracle must fail (else remove it)."""
        step = self.step(
            words, exits=frozenset({0, 1}), cells=(cell,), model=requested.model(), mutating=False
        )
        output = step.result.stdout + step.result.stderr
        assert not re.search(_ORG_BLOCK_NAMED, output), (
            f"{cell} now names the org block: remove it from KNOWN_PLUGIN "
            f"({KNOWN_PLUGIN[cell]})\n{output}"
        )
        # Only the tracked misleading approval counts; any other outcome is a new cause.
        tracked = (
            step.result.returncode == 0
            and re.search(_APPROVED, output)
            and re.search(_INSTALL_HINT, " ".join(output.split()))
        )
        assert tracked, (
            f"{cell} no longer shows the approval {KNOWN_PLUGIN[cell]} tracks "
            f"(exit {step.result.returncode}): re-key or remove the cell\n{output}"
        )
        assert set(step.after.deployed) == set(step.before.deployed), cell
        self.expected_seen.add(cell)

    def _requested(self, op: str, retires: bool = False) -> PluginBinState:
        state = replace(self.state)
        if op == "install":
            state.declared = True
            state.deployed = state.allows()
        elif op in CONTEXT_INSTALLS or op == MULTI_INSTALL:
            # A re-install of the converged plugin keeps exactly what it had.
            assert state.declared and state.deployed == state.allows(), (op, state)
        if op == "uninstall":
            state.declared = state.revoked = state.deployed = False
        if op == "approve" and not state.denied_by_policy:
            # An org deny refuses the approval and records nothing (_gate_kit).
            state.gate_on, state.decision, state.revoked = True, "allowed", False
        if op == "deny":
            state.gate_on, state.decision, state.revoked = True, "denied", state.deployed
        if retires:
            # A retired plugin is gone: what stays is the decision, not a revocation.
            state.deployed = state.revoked = False
        return state

    def _check_outcome(self, op: str, requested: PluginBinState, step: Step, retires: bool) -> None:
        """What I1-I9 cannot see: the whole root and its declarations go or stay together."""
        if op == "pack":
            self._check_pack(step)
            return
        after = set(step.after.deployed)
        wanted = {"/".join(c) for c in expected_deployed(requested.model(), KINDS)}
        if op == "install":
            assert after == wanted, f"install: deployed {sorted(after)}, model {sorted(wanted)}"
            if after:
                self.root_files = self._root_files()
                assert BIN_PATH in self.root_files, sorted(self.root_files)
                # A claude-only plugin is declared to Claude Code and pinned off for
                # Copilot, so each settings file holds a key prune must remove.
                files = {entry.split(":", 1)[0] for entry in self._declarations()}
                assert files == set(SETTINGS), f"install declared it in {sorted(files)}"
            return
        if op == "mcp-install":
            # U7 keeps bin/ (below); the command itself must still install the server.
            servers = _mcp_servers(self.bench.deploy_root)
            problems = mcp_install_problems(
                self._mcp_before or frozenset(),
                servers,
                self.bench.manifest(),
                step.after.lock,
                self._mcp_before or frozenset(),
            )
            assert not problems, f"mcp-install: {problems}"
        if op == "uninstall":
            self._check_uninstalled(step)
        if not retires:
            assert after == set(step.before.deployed), f"{op} changed bin/: {sorted(after)}"
            return
        root = self.bench.deploy_root / ROOT
        assert not root.exists(), f"prune kept {ROOT}: {sorted(self._root_files())}"
        assert not self._declarations(), f"prune kept declarations: {self._declarations()}"
        declared = (self.bench.manifest().get("dependencies") or {}).get("apm") or []
        assert bool(declared) == requested.declared, f"prune changed apm.yml: {declared}"
        lock, before = step.after.lock, step.before.lock
        assert before is not None, "prune ran without a readable lockfile"
        kept = {k for k in before.dependencies if PLUGIN in k}
        others = set(before.dependencies) - kept
        if not requested.declared and not others:
            # prune.md: a prune that leaves zero dependencies deletes apm.lock.yaml.
            assert not self.bench.lock_path.exists(), "orphan prune kept an empty lockfile"
            return
        assert lock is not None, "prune left no readable lockfile"
        rows = {k for k in lock.dependencies if PLUGIN in k}
        if not requested.declared:
            assert not rows, f"orphan prune kept lock rows: {sorted(rows)}"
            return
        # A gate retirement keeps the declared dependency's row, minus its record.
        assert kept and rows == kept, f"prune changed the rows: {sorted(kept)} -> {sorted(rows)}"
        records = [k for k in rows if lock.dependencies[k].native_plugin]
        assert not records, f"prune kept the native plugin record: {records}"

    def _check_pack(self, step: Step) -> None:
        """Pack leaves the whole plugin out and says so once, with no looping remedy (#5253)."""
        output = f"{step.result.stdout}\n{step.result.stderr}"
        listed = {line.strip() for line in step.result.stdout.splitlines()}
        assert not any(f"bin/{PLUGIN}" in line for line in listed), step.result.stdout
        named = [line for line in output.splitlines() if f"lifecycle/{PLUGIN}" in line]
        assert len(named) == 1, f"want one line naming the native plugin: {named}"
        assert named[0].lstrip().startswith("[!]"), named[0]
        assert "'apm install' to record provenance" not in output, output

    def tripwire_bundle(self) -> None:
        """A consumer of the project's bundle gets nothing of the omitted plugin (#5253).

        The bundle's own lock names no ``native_plugin`` root it does not carry, and
        installing the bundle deploys nothing under ``.agents/plugins`` and records no
        such row.
        """
        if self.dry:
            return
        world, project = self.bench.world, self.bench.deploy_root
        before = {p.relative_to(project).as_posix() for p in project.rglob("*")}
        out = world.root / "tripwire-bundle"
        packed = self.bench.run(("pack", "--output", str(out)))
        assert packed.returncode == 0, packed.stdout + packed.stderr
        # Pack synthesizes the project's plugin manifest; nothing else in it changes.
        added = {p.relative_to(project).as_posix() for p in project.rglob("*")} - before
        assert added <= {".claude-plugin", ".claude-plugin/plugin.json"}, sorted(added)
        if ".claude-plugin" in added:
            shutil.rmtree(project / ".claude-plugin")
        (bundle,) = [path for path in out.iterdir() if path.is_dir()]
        rows = yaml.safe_load((bundle / "apm.lock.yaml").read_text(encoding="utf-8"))
        carried = [row for row in rows.get("dependencies") or [] if "native_plugin" in row]
        assert not carried, f"the bundle lock names a root it does not carry: {carried}"
        consumer = world.root / "tripwire-consumer"
        consumer.mkdir()
        manifest = "name: consumer\nversion: 1.0.0\ntargets: [claude]\ndependencies:\n  apm: []\n"
        (consumer / "apm.yml").write_text(manifest, encoding="utf-8")
        runner = ApmLifecycleRunner(driver.driver_command(), timeout_seconds=driver.TIMEOUT_SECONDS)
        result = runner.run(
            ("install", str(bundle)), cwd=consumer, env=world.env, scenario_id="tripwire-bundle"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (consumer / ".agents").exists(), sorted(map(str, consumer.rglob("*")))
        lock = consumer / "apm.lock.yaml"
        text = lock.read_text(encoding="utf-8") if lock.exists() else ""
        assert "native_plugin" not in text, text
        shutil.rmtree(out)
        shutil.rmtree(consumer)

    def _check_uninstalled(self, step: Step) -> None:
        """Uninstall drops the declaration and the lock row a prune kept, not only bin/."""
        before = step.before.lock
        assert before is not None and any(PLUGIN in k for k in before.dependencies), (
            "uninstall ran with no lock row for the plugin to remove"
        )
        declared = (self.bench.manifest().get("dependencies") or {}).get("apm") or []
        assert not [d for d in declared if PLUGIN in str(d)], f"uninstall kept {declared}"
        lock = step.after.lock
        rows = [k for k in lock.dependencies if PLUGIN in k] if lock is not None else []
        assert not rows, f"uninstall kept lock rows: {rows}"

    def _credit(
        self,
        op: str,
        cell: str,
        step: Step,
        retires: bool,
        exits: frozenset[int],
        says: tuple[str, ...],
    ) -> None:
        present: dict[str, list[str]] = {}
        absent: dict[str, list[str]] = {}
        unchanged: dict[str, tuple[str, ...]] = {"outside": ("*",)}
        matches: tuple[str, ...] = says
        if retires:
            absent["project"] = sorted(self.root_files)
            matches = (*says, _RETIRED)
        elif op != "install":
            # A catalog command leaves every project byte alone; approve leaves the root.
            whole = op in ("marketplace", "install-dry-run", "pack")
            unchanged["project"] = ("*",) if whole else (ROOT, *SETTINGS)
        if not retires and step.after.deployed:
            present["project"] = [BIN_PATH]
        evidence = Evidence(
            absent=absent,
            present=present,
            unchanged=unchanged,
            output_matches=matches,
            exit=exits,
        )
        check_obligation(
            cell,
            strength="e2e-evidence",
            before=step.before,
            after=step.after,
            result=step.result,
            evidence=evidence,
        )

    def _root_files(self) -> frozenset[str]:
        root = self.bench.deploy_root / ROOT
        if not root.is_dir():
            return frozenset()
        return frozenset(
            path.relative_to(self.bench.deploy_root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )

    def _declarations(self) -> list[str]:
        """``enabledPlugins`` keys of the plugin in every harness settings file."""
        found = []
        for name in SETTINGS:
            path = self.bench.deploy_root / name
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            keys = (data.get("enabledPlugins") or {}).keys()
            found += [f"{name}:{key}" for key in keys if key.split("@", 1)[0] == PLUGIN]
        return found

    # -- input edits (no command, no credit) -----------------------------------

    def _edit_manifest(self, name: str, change) -> None:
        if self.dry:
            return

        def apply() -> None:
            data = self.bench.manifest()
            change(data)
            self.bench.write_manifest(data)

        self.edit(name, apply)

    def setup(self) -> None:
        """Target claude with no executables block: no layer opts into the gate."""

        def change(data: dict) -> None:
            data["targets"] = sorted(TARGETS)
            data.pop("executables", None)

        self._edit_manifest("claude-gate-off", change)

    def undeclare(self) -> None:
        self._edit_manifest("undeclare-" + PLUGIN, lambda data: data["dependencies"].update(apm=[]))
        self.state.declared = False

    def set_targets(self, *targets: str) -> None:
        """Declare *targets* in apm.yml (an input edit); the model and the observer follow."""

        def change(data: dict) -> None:
            data["targets"] = sorted(targets)

        self._edit_manifest("targets-" + "-".join(sorted(targets)), change)
        self.state.targets = frozenset(targets)
        if not self.dry:
            self.model = replace(self.model, targets=frozenset(targets))

    def gate_on(self) -> None:
        """An empty executables block turns the gate on over the deployed plugin."""
        self._edit_manifest("gate-on", lambda data: data.update(executables={}))
        self.state.gate_on = True

    def deny_by_policy(self) -> None:
        if not self.dry:
            self.edit("policy-" + DENY_POLICY, lambda: use_policy(self.bench.world, DENY_POLICY))
        self.state.denied_by_policy = True

    def spec(self) -> str:
        return "" if self.dry else self.bench.world.packages[f"{PLUGIN}:git"].spec

    def marketplace_url(self) -> str:
        return "" if self.dry else self.bench.world.services.marketplace_url


def replay_plugin_bin(machine: GatePluginBin) -> None:
    """Deploy the plugin whole, then let prune retire it at each gate level."""
    install = ("install", "--trust-bin")
    machine.setup()
    # none: audit --ci replays the deployed root, bin/ included, and changes nothing;
    # then an orphaned plugin is pruned whole.
    machine.run("install", *install, machine.spec())
    machine.run("audit-ci", "audit", "--ci")
    # pack leaves the whole plugin out, naming it once (#5253).
    machine.run("pack", "pack", "--dry-run")
    machine.undeclare()
    machine.run("prune", "prune", retires=True)
    # unapproved-exec: the gate turns on over a plugin deployed with it off.
    machine.run("install", *install, machine.spec())
    machine.gate_on()
    machine.run("audit-ci", "audit", "--ci", exits=AUDIT_GATED, says=(_STALE_DECISION,))
    machine.run("prune", "prune", retires=True)
    # allowed: audit replays the approved root and prune keeps it.
    machine.run("approve", "approve", PLUGIN)
    machine.run("install", *install)
    machine.run("audit-ci", "audit", "--ci")
    machine.run("prune", "prune")
    for op, (flags, _factors) in CONTEXT_INSTALLS.items():
        machine.run(op, *install, *flags)
    # Widen to claude and copilot (a target change), re-install unchanged, narrow back.
    machine.set_targets("claude", "copilot")
    machine.run("install", *install)
    machine.run(MULTI_INSTALL, *install)
    machine.set_targets(*sorted(TARGETS))
    machine.run("install", *install)
    # revoked-after-deploy: a deny over the deployed root; prune retires it.
    machine.run("deny", "deny", PLUGIN)
    machine.run("audit-ci", "audit", "--ci", exits=AUDIT_GATED, says=(_STALE_DECISION,))
    machine.run("prune", "prune", retires=True)
    # denied-executable: install deploys nothing; audit and prune have nothing to act on.
    machine.run("install", *install)
    machine.run("audit-ci", "audit", "--ci")
    machine.run("prune", "prune")
    machine.run("approve", "approve", PLUGIN)
    machine.run("install", *install)
    # pack leaves the whole plugin out, naming it once (#5253).
    machine.run("pack", "pack", "--dry-run")
    machine.tripwire_bundle()
    # denied-dependency: org policy denies the approved, deployed plugin.
    machine.deny_by_policy()
    machine.run("marketplace", "marketplace", "add", machine.marketplace_url())
    machine.run("audit-ci", "audit", "--ci", exits=AUDIT_DENIED, says=(_DENYLISTED,))
    machine.run("approve", "approve", PLUGIN)
    machine.run("compile", "compile", exits=COMPILE_DENIED, says=(_COMPILE_BLOCKED,))
    machine.run("lock", "lock", exits=LOCK_DENIED, says=(_RESOLVE_BLOCKED,))
    machine.run("pack", "pack", "--dry-run", exits=(1,), says=(_PACK_BLOCKED,))
    machine.run("mcp-install", "install", "--mcp", REGISTRY_SERVER)
    machine.run("prune", "prune", retires=True)
    machine.run("uninstall", "uninstall", machine.spec())


# Re-installs of the deployed, approved plugin that differ only by a modifier: each keeps
# bin/ byte-identical (the dry run writes nothing at all), so each proves PRIM=bin with
# that modifier on an online, single-unchanged-target project whose bin/ is clean.
CONTEXT_INSTALLS: Mapping[str, tuple[tuple[str, ...], Mapping[str, str]]] = {
    "install-frozen": (("--frozen",), {"FROZEN": "on"}),
    "install-update": (("--update",), {"UPDATE": "on"}),
    "install-force": (("--force",), {"FORCE": "on"}),
    "install-dry-run": (("--dry-run",), {"DRYRUN": "on"}),
    "install-select": (("--target", "claude"), {"SELECT": "on"}),
}
# A converged re-install with claude and copilot both declared keeps bin/.
MULTI_INSTALL = "install-multi"
_CONTEXT = {"DEPLOYED": "clean", "NET": "online", "TGT": "single-unchanged"}


def bin_context(cell: str) -> Mapping[str, str] | None:
    """The factor context a step of this spine proves for PRIM=bin at project scope.

    A deploying install (gate off or allowed) proves bin/ deployed; a context
    re-install of the deployed plugin also proves its modifier. Any other step,
    or one under another gate level, proves no bin context (``None``).
    """
    parts = cell.split("|")
    op, gate = parts[1], parts[3]
    if op == "install" and gate in ("none", "allowed"):
        return {}
    if op in CONTEXT_INSTALLS and gate == "allowed":
        return {**_CONTEXT, **CONTEXT_INSTALLS[op][1]}
    if op == MULTI_INSTALL and gate == "allowed":
        return {**_CONTEXT, "TGT": "multi-unchanged"}
    return None


def plugin_bin_cells() -> frozenset[str]:
    """Every machine id the plugin bin spine credits (labels only, no CLI)."""
    machine = GatePluginBin(None)
    replay_plugin_bin(machine)
    return frozenset(machine.credited)


def deploys_bin(cell: str) -> bool:
    """Whether the step behind *cell* deploys (or keeps) the plugin's bin/ at project scope."""
    return bin_context(cell) is not None
