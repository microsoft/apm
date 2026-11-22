"""LOCK states across the lifecycle factors (#4097).

PROMISE: P-OWN-lockfile-generator-compatibility -- a lockfile a newer APM wrote
is refused by every resolving command before anything changes (source:
src/apm_cli/deps/lockfile_generator.py).
PROMISE: P-INV-I1 / P-INV-I4 -- a stale lockfile (the manifest moved past it)
and a missing one reconcile on the next install: every declared package deploys
at the declared targets, gates stay monotone and the user's bytes survive.
PROMISE: P-INV-I3 -- ``install --frozen`` refuses a stale lockfile (req-lk-006).

T-3 crosses LOCK with the other factors. Each test converges one world, then
before every command turns its lockfile into one drifted state by hand (an
input edit, never a command): ``stale`` flips a dependency in apm.yml,
``absent`` deletes apm.lock.yaml, ``newer-generator`` stamps APM 99.0.0 into
it. The command runs through the scenario kit (I1-I9 every step) and credits
the pairs its assignment implies. A newer lockfile's refusal credits only
through ``_kit.EXPECTED_REFUSALS`` (NEWER_LOCK), with two-factor assignments,
so a refusal never discharges a success cell. ``consistent`` pairs are
credited where a converged world already runs a command
(test_target_history_sources, test_ownership_siblings, test_policy_pairs).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.scenarios._kit import (
    PROJECT_HOME_ALLOWED,
    Scenario,
    credits,
    mcp_server_problems,
)
from tests.lifecycle.scenarios.test_network_offline import go_offline
from tests.lifecycle.scenarios.test_target_history_sources import (
    _CATALOG_CACHE,
    _register_marketplaces,
)
from tests.lifecycle.world import use_policy, use_registry
from tests.lifecycle.world.packages import INERT_COMMAND

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I3",
        "P-INV-I4",
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-lockfile-generator-compatibility",
    ),
]

NEWER = "newer-generator"
_SKILL = ".agents/skills/p-skill/SKILL.md"
_KEEP = {"project": (_SKILL,)}
_TOGGLE = "p-child"
_DENY = "deny-all-deps"
_NO_POLICY_SAYS = r"Policy enforcement disabled by --no-policy for this invocation"
_USER_SKILL = "---\nname: p-skill\ndescription: The user's own p-skill.\n---\n\nMine.\n"


def _pair(lock: str, **factors: str) -> dict[str, str]:
    return {"LOCK": lock, **factors}


@dataclass(frozen=True)
class Row:
    """Input edits, then one command and the pairs it proves (a declared refusal or not)."""

    edits: tuple[str, ...]
    line: str
    credit: tuple[Mapping[str, str], ...]
    refusal: bool = False
    says: tuple[str, ...] = ()


# -- input edits -------------------------------------------------------------------


def stamp_newer(scenario: Scenario) -> None:
    """A newer APM wrote the lockfile (it records its generator once)."""
    path = scenario.lock_path
    text, count = re.subn(
        r"^apm_version: .*$", "apm_version: 99.0.0", path.read_text("utf-8"), flags=re.M
    )
    assert count == 1, "the lock records its generator once"
    path.write_text(text, encoding="utf-8")
    scenario.model = scenario.model.with_lock(NEWER)


def forget_lock(scenario: Scenario) -> None:
    """The lockfile is gone (a checkout without it)."""
    if scenario.lock_path.exists():
        scenario.lock_path.unlink()
    scenario.model = scenario.model.with_lock("absent")


def toggle(scenario: Scenario, name: str = _TOGGLE) -> None:
    """Make apm.yml disagree with the lockfile on *name* (flip it only while they agree)."""
    lock = scenario.lock_path
    in_lock = lock.exists() and name in lock.read_text(encoding="utf-8")
    if (name in scenario.roots) == in_lock:
        if name in scenario.roots:
            scenario.drop_from_manifest(name)
        else:
            manifest = scenario.manifest_root / "apm.yml"
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            dependencies = data.get("dependencies") or {}
            dependencies["apm"] = [*(dependencies.get("apm") or []), scenario.specs[name]]
            data["dependencies"] = dependencies
            manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
            scenario.adopt(name)
    scenario.model = scenario.model.with_lock("stale" if lock.exists() else "absent")


def _policy(scenario: Scenario, variant: str) -> None:
    use_policy(scenario.world, variant)
    denied = tuple(scenario.roots) if variant == _DENY else ()
    scenario.model = scenario.model.with_policy(variant, denied)


# What a fresh checkout lacks: the lockfile and every deployed surface (git ignores them).
_DEPLOYED_SURFACES = (".agents", ".claude", ".github/hooks", ".github/extensions", "apm_modules")
_DEPLOYED_FILES = (".github/lsp.json", ".mcp.json")


def checkout(scenario: Scenario) -> None:
    """A fresh clone: apm.yml (with its approvals) but no lockfile and nothing deployed."""
    root = scenario.deploy_root
    for name in _DEPLOYED_SURFACES:
        shutil.rmtree(root / name, ignore_errors=True)
    for name in _DEPLOYED_FILES:
        (root / name).unlink(missing_ok=True)
    forget_lock(scenario)


EDITS = {
    "checkout": checkout,
    "toggle": toggle,
    "forget": forget_lock,
    "stamp": stamp_newer,
    "deny": lambda scenario: _policy(scenario, _DENY),
    "allow": lambda scenario: _policy(scenario, "allow"),
    "offline": lambda scenario: go_offline(scenario.world),
}
_DRIFT = {"stale": "toggle", "absent": "forget", NEWER: "stamp"}


def play(scenario: Scenario, rows, *, keep=None, **fields: object) -> None:
    """Apply each row's edits, run its command and credit its pairs."""
    for row in rows:
        for edit in row.edits:
            EDITS[edit](scenario)
        scenario.source_moved()
        exit = 1 if row.refusal else None
        step = scenario.run(
            row.line,
            exit=exit,
            credit=row.credit,
            refusal=row.refusal,
            says=row.says,
            keep=keep,
            **fields,
        )
        assert step.result.returncode == (1 if row.refusal else 0), step.output
        if not row.refusal and any(item.get("GATE") == "allowed" for item in row.credit):
            # An allowed-gate credit needs its own evidence: this step deployed the
            # approved executables and wrote the lock (a later install cannot repair it).
            assert scenario.lock_path.is_file(), step.output
            scenario.assert_deployed()


def _all_credits(rows) -> list[Mapping[str, str]]:
    return [item for row in rows for item in row.credit]


# -- executable gates, modifiers, network and policy ---------------------------------

# Package -> the gate level its executables hold after ``_gated`` (one PRIM each).
_GATES = {"p-canvas": "allowed", "p-exec": "denied-executable", "p-lsp": "revoked-after-deploy"}
_GATED = ("p-skill", _TOGGLE, *sorted(_GATES))


def _gate_pairs(lock: str) -> tuple[dict[str, str], ...]:
    gates = tuple(_pair(lock, GATE=gate) for gate in sorted(set(_GATES.values())))
    return (*gates, _pair(lock, PRIM="hook"), _pair(lock, PRIM="canvas"))


def _gated_rows(lock: str) -> tuple[Row, ...]:
    says = (_NO_POLICY_SAYS,)
    if lock == "stale":
        return (
            Row(("toggle", "deny"), "install", (_pair(lock, GATE="denied-dependency"),), True),
            Row(("allow",), "install", _gate_pairs(lock)),
            Row(("toggle",), "install --dry-run", (_pair(lock, DRYRUN="on"),)),
            Row((), "install --no-policy", (_pair(lock, NOPOLICY="on"),), says=says),
            # --frozen keeps a package apm.yml dropped; one it adds has no pin and refuses.
            Row(("toggle",), "install --frozen", (_pair(lock, FROZEN="on"),), True),
            Row(("toggle",), "install --force", (_pair(lock, FORCE="on"),)),
            Row(("toggle",), "install --update", (_pair(lock, UPDATE="on"),)),
            Row(("toggle", "offline"), "install", (_pair(lock, NET="offline"),)),
        )
    if lock == "absent":
        return (
            Row(("checkout",), "install", _gate_pairs(lock)),
            Row(("checkout",), "install --no-policy", (_pair(lock, NOPOLICY="on"),), says=says),
            Row(("checkout", "offline"), "install", (_pair(lock, NET="offline"),)),
        )
    flags = (
        ("install --dry-run", "DRYRUN"),
        ("install --force", "FORCE"),
        ("install --update", "UPDATE"),
        ("install --no-policy", "NOPOLICY"),
    )
    return (
        Row(("stamp",), "install", _gate_pairs(lock), True),
        *(Row((), line, (_pair(lock, **{flag: "on"}),), True) for line, flag in flags),
        Row(("deny",), "install", (_pair(lock, GATE="denied-dependency"),), True),
        Row(("allow", "offline"), "install", (_pair(lock, NET="offline"),), True),
    )


def _gated(world) -> Scenario:
    """Canvas approved, exec denied while parked, LSP approved, deployed, then denied."""
    use_policy(world, "allow")
    scenario = Scenario(world, _GATED, policy="allow")
    scenario.run("install p-skill p-exec p-canvas p-lsp")
    scenario.run("approve p-canvas p-lsp")
    scenario.run("install")
    scenario.run("deny p-exec p-lsp")
    return scenario


_LOCKS = ("stale", "absent", NEWER)


@pytest.mark.parametrize("lock", _LOCKS)
def test_gates_modifiers_and_network_under_a_drifted_lock(lifecycle_world, lock) -> None:
    scenario = _gated(lifecycle_world)
    play(scenario, _gated_rows(lock))
    if lock != NEWER:
        scenario.assert_deployed()
    scenario.finish()


# -- the deployed state the user left ----------------------------------------------

_DEPLOYED_CASES = (
    ("stale", "absent"),
    ("stale", "edited"),
    ("stale", "unowned"),
    (NEWER, "absent"),
    (NEWER, "edited"),
    (NEWER, "unowned"),
    ("absent", "unowned"),
)


def _deployed(world, lock: str, deployed: str) -> Scenario:
    """p-skill installed; its skill edited or the user's own; or p-child to deploy (absent)."""
    use_policy(world, "allow")
    scenario = Scenario(world, ("p-skill", _TOGGLE), policy="allow")
    if deployed == "unowned":
        scenario.user_write(_SKILL, _USER_SKILL)
        if lock == "absent":
            toggle(scenario)  # declared by hand: a checkout with no lockfile
        else:
            scenario.run(f"install {_TOGGLE}")
        return scenario
    if deployed == "absent":
        # p-child is declared by hand next (the drift edit): nothing of it is deployed.
        scenario.run("install p-skill")
        return scenario
    scenario.run(f"install p-skill {_TOGGLE}")
    edited = (world.project / _SKILL).read_text(encoding="ascii") + "\nMy own note.\n"
    scenario.user_write(_SKILL, edited)
    return scenario


_CHILD_SKILL = ".agents/skills/p-child/SKILL.md"


@pytest.mark.parametrize(
    ("lock", "deployed"), _DEPLOYED_CASES, ids=[f"{lock}-{dep}" for lock, dep in _DEPLOYED_CASES]
)
def test_deployed_state_under_a_drifted_lock(lifecycle_world, lock, deployed) -> None:
    scenario = _deployed(lifecycle_world, lock, deployed)
    refused = lock == NEWER
    if (lock, deployed) == ("absent", "unowned"):
        drift: tuple[str, ...] = ()
    elif deployed == "absent":
        drift = ("toggle", "stamp") if refused else ("toggle",)
    else:
        drift = (_DRIFT[lock],)
    row = Row(drift, "install", (_pair(lock, DEPLOYED=deployed),), refused)
    play(scenario, (row,), keep=_KEEP)
    if deployed == "absent":
        deployed_now = (lifecycle_world.project / _CHILD_SKILL).is_file()
        assert deployed_now != refused, "p-child deploys unless the newer lock refuses"
    scenario.finish()


# -- a target change ------------------------------------------------------------------

# Lock -> (declared targets at the start, [(TGT, targets after the edit)]). A
# stale or absent chain reconciles each step, so the next shape starts there; a
# newer lockfile refuses, so every edit starts from the deployed targets.
_TARGET_CHAINS = {
    "stale": (
        ("copilot",),
        (
            ("single-narrowed", ("claude",)),
            ("single-widened", ("claude", "copilot")),
            ("multi-unchanged", ("claude", "copilot")),
            ("multi-widened", ("claude", "copilot", "gemini")),
            ("multi-narrowed", ("copilot",)),
        ),
    ),
    "absent": (
        ("copilot",),
        (
            ("single-narrowed", ("claude",)),
            ("single-widened", ("claude", "copilot")),
            ("multi-widened", ("claude", "copilot", "gemini")),
            ("multi-narrowed", ("copilot",)),
        ),
    ),
    f"{NEWER}-single": (
        ("copilot",),
        (("single-narrowed", ("claude",)), ("single-widened", ("copilot", "claude"))),
    ),
    f"{NEWER}-multi": (
        ("copilot", "claude"),
        (
            ("multi-unchanged", ("copilot", "claude")),
            ("multi-widened", ("copilot", "claude", "gemini")),
            ("multi-narrowed", ("copilot",)),
        ),
    ),
}


def _target_lock(chain: str) -> str:
    return NEWER if chain.startswith(NEWER) else chain


def _target_credit(chain: str) -> list[dict[str, str]]:
    lock = _target_lock(chain)
    return [_pair(lock, TGT=tgt) for tgt, _after in _TARGET_CHAINS[chain][1]]


@pytest.mark.parametrize("chain", sorted(_TARGET_CHAINS))
def test_target_change_under_a_drifted_lock(lifecycle_world, chain) -> None:
    start, steps = _TARGET_CHAINS[chain]
    lock = _target_lock(chain)
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, ("p-skill", _TOGGLE), targets=start, policy="allow")
    scenario.run("install p-skill")
    if lock == NEWER:
        stamp_newer(scenario)
    for tgt, after in steps:
        if lock == NEWER:
            scenario.set_targets(*start)
        else:
            EDITS[_DRIFT[lock]](scenario)
        scenario.set_targets(*after)
        play(scenario, (Row((), "install", (_pair(lock, TGT=tgt),), lock == NEWER),))
        if lock != NEWER:
            scenario.assert_deployed(unproven_kept=lock == "absent")
    scenario.finish()


# -- package sources ------------------------------------------------------------------

_INLINE = ("install", "--mcp", "p-inline", "--", INERT_COMMAND, "--stdio")
# World -> the sources whose packages it declares.
_SOURCE_WORLDS = {
    "direct": ("git", "inline-mcp", "native-plugin"),
    "registry": ("pkg-registry",),
    "marketplace": ("marketplace-file", "marketplace-git", "marketplace-local"),
}
_SOURCE_CASES = tuple((world, lock) for world in _SOURCE_WORLDS for lock in _LOCKS)
_MARKET_HOME = {"home": (*PROJECT_HOME_ALLOWED, *_CATALOG_CACHE)}


def _source_credit(world: str, lock: str) -> list[dict[str, str]]:
    return [_pair(lock, SRC=src) for src in _SOURCE_WORLDS[world]]


def _sources(world, name: str) -> tuple[Scenario, dict[str, object]]:
    """(scenario, run fields) with every package of the source world installed."""
    use_policy(world, "allow")
    if name == "marketplace":
        specs = _register_marketplaces(world)
        specs[_TOGGLE] = world.packages[_TOGGLE].spec
        scenario = Scenario(world, specs, policy="allow")
        names = " ".join(name for name in specs if name != _TOGGLE)
        scenario.run(f"install {names}", allowed=_MARKET_HOME)
        return scenario, {"allowed": _MARKET_HOME}
    if name == "registry":
        # The registry becomes the default for shorthand specs, so it gets its own world.
        use_registry(world)
        specs = {"p-skill": world.packages["p-skill:registry"].spec}
    else:
        specs = {
            "p-skill": world.packages["p-skill:git"].spec,
            "p-native-claude": world.packages["p-native-claude"].spec,
        }
    specs[_TOGGLE] = world.packages[_TOGGLE].spec
    targets = ("copilot", "claude")
    scenario = Scenario(world, specs, targets=targets, policy="allow")
    scenario.run(f"install {' '.join(name for name in specs if name != _TOGGLE)}")
    if name == "direct":
        scenario.source_moved()  # the user adds an MCP server
        scenario.run(_INLINE)
    return scenario, {}


@pytest.mark.parametrize(
    ("world", "lock"), _SOURCE_CASES, ids=[f"{world}-{lock}" for world, lock in _SOURCE_CASES]
)
def test_sources_under_a_drifted_lock(lifecycle_world, world, lock) -> None:
    scenario, fields = _sources(lifecycle_world, world)
    drift = "checkout" if lock == "absent" else _DRIFT[lock]
    row = Row((drift,), "install", tuple(_source_credit(world, lock)), lock == NEWER)
    play(scenario, (row,), **fields)
    if lock != NEWER:
        scenario.assert_deployed()
        if world == "direct":
            # The root inline server apm.yml declares comes back with the rest.
            problems = mcp_server_problems(scenario, {"p-inline"})
            assert not problems, problems
    scenario.finish()


# -- a plugin's bin/ at user scope (pack: test_pack_lock_states) ---------------------

_BIN_LOCKS = ("stale", NEWER)


_BIN = "p-native-claude-bin"


def _bin_credit(lock: str) -> list[dict[str, str]]:
    found = [_pair(lock, PRIM="bin")]
    return found if lock == NEWER else [*found, {"PRIM": "bin", "SCOPE": "user"}]


@pytest.mark.parametrize("lock", _BIN_LOCKS)
def test_user_bin_plugin_under_a_drifted_lock(lifecycle_world, lock) -> None:
    """bin/ deploys only from a marketplace plugin at user scope (U-BIN)."""
    world = lifecycle_world
    specs = {
        _BIN: world.packages[f"{_BIN}:marketplace"].spec,
        _TOGGLE: world.packages[_TOGGLE].spec,
    }
    add = driver.run(world, ("marketplace", "add", world.services.marketplace_url))
    assert add.returncode == 0, add.stdout + add.stderr
    scenario = Scenario(world, specs, scope="user", targets=("claude",))
    scenario.run(f"install {_BIN} --trust-bin")
    scenario.run(f"approve {_BIN}")
    scenario.run("install --trust-bin")
    scenario.assert_deployed()
    drift = "stamp" if lock == NEWER else "toggle"
    row = Row((drift,), "install --trust-bin", tuple(_bin_credit(lock)), lock == NEWER)
    play(scenario, (row,))
    if lock != NEWER:
        scenario.assert_deployed()
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        **{
            f"test_gates_modifiers_and_network_under_a_drifted_lock[{lock}]": _all_credits(
                _gated_rows(lock)
            )
            for lock in _LOCKS
        },
        **{
            f"test_deployed_state_under_a_drifted_lock[{lock}-{dep}]": [_pair(lock, DEPLOYED=dep)]
            for lock, dep in _DEPLOYED_CASES
        },
        **{
            f"test_target_change_under_a_drifted_lock[{chain}]": _target_credit(chain)
            for chain in _TARGET_CHAINS
        },
        **{
            f"test_sources_under_a_drifted_lock[{world}-{lock}]": _source_credit(world, lock)
            for world, lock in _SOURCE_CASES
        },
        **{
            f"test_user_bin_plugin_under_a_drifted_lock[{lock}]": _bin_credit(lock)
            for lock in _BIN_LOCKS
        },
    },
)
