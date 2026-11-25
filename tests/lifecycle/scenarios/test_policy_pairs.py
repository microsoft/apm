"""Org dependency policy and ``--no-policy`` across the lifecycle factors (#4108).

PROMISE: P-INV-I4 -- an org policy that denies a declared dependency never
widens what is deployed: every resolving install refuses with exit 1 and
changes nothing, ``--dry-run`` says what would be blocked, and ``audit --ci``
fails while the denied package stays declared (source:
docs/src/content/docs/reference/cli/install.md, policy/install_preflight.py).
PROMISE: P-INV-I7 -- ``--no-policy`` is announced: the install proceeds and
says that enforcement is off for this run only.

T-7b crosses GATE=denied-dependency and NOPOLICY=on with the other factors.
Refusals earn credit only through the declared table
(``_kit.EXPECTED_REFUSALS``: ``POLICY_REFUSALS``); a declared user write must
keep its bytes (I1-X2).
"""

from __future__ import annotations

import re

import pytest

from tests.lifecycle import driver
from tests.lifecycle.scenarios._kit import (
    Known,
    Scenario,
    assert_changes,
    credits,
    known_defect,
)
from tests.lifecycle.scenarios._sources import (
    MARKETPLACES,
    SOURCES,
    from_source,
    source_fields,
)
from tests.lifecycle.world import use_policy
from tests.lifecycle.world.packages import INERT_COMMAND
from tests.lifecycle.world.services import REGISTRY_SERVER
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_snapshot_set_unchanged

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I4", "P-INV-I7"),
]

_SKILL = ".agents/skills/p-skill/SKILL.md"
_KEEP = {"project": (_SKILL,)}
_DENY = "deny-all-deps"
_DD = "denied-dependency"
_NO_POLICY_SAYS = r"Policy enforcement disabled by --no-policy for this invocation"
_WOULD_BLOCK = r"Would be blocked by policy: \S*p-skill -- denied by pattern"
_USER_SKILL = "---\nname: p-skill\ndescription: The user's own p-skill.\n---\n\nMine.\n"


def _pair(**factors: str) -> dict[str, str]:
    return dict(factors)


def _deny(scenario: Scenario, *names: str, variant: str = _DENY) -> None:
    """The org starts denying *names* (an input outside the CLI)."""
    use_policy(scenario.world, variant)
    scenario.model = scenario.model.with_policy(variant, names)
    scenario.source_moved()


def _forget_lock(scenario: Scenario) -> None:
    scenario.lock_path.unlink()
    scenario.model = scenario.model.with_lock("absent")


def _skill(world, deployed: str = "clean") -> Scenario:
    """Local p-skill under an allowing org policy, in *deployed* state, then denied."""
    use_policy(world, "allow")
    scenario = Scenario(world, ("p-skill",), policy="allow")
    if deployed == "unowned":
        scenario.user_write(_SKILL, _USER_SKILL)
    else:
        scenario.run("install p-skill")
        text = (world.project / _SKILL).read_text(encoding="ascii")
        if deployed != "clean":
            edited = text + "\nMy own note.\n" if deployed == "edited" else text
            scenario.user_write(_SKILL, edited)
        if deployed == "present-provenance-unknown":
            _forget_lock(scenario)
    _deny(scenario, "p-skill")
    return scenario


# -- a denied dependency, every modifier ---------------------------------------------

_REFUSED_LINES = {
    "plain": ("install", {"SRC": "local-path"}),
    "force": ("install --force", {"FORCE": "on"}),
    "update": ("install --update", {"UPDATE": "on"}),
    "select": ("install p-skill", {"SELECT": "on"}),
}


@pytest.mark.parametrize("row", sorted(_REFUSED_LINES))
def test_a_denied_dependency_refuses_every_resolving_install(lifecycle_world, row) -> None:
    scenario = _skill(lifecycle_world)
    line, factors = _REFUSED_LINES[row]
    step = scenario.run(line, credit=[_pair(GATE=_DD, **factors)], keep=_KEEP, refusal=True)
    assert "Blocked by org policy" in " ".join(step.output.split()), step.output
    scenario.finish()


def test_a_dry_run_says_what_the_policy_would_block(lifecycle_world) -> None:
    scenario = _skill(lifecycle_world)
    # A preview reports the block and exits 0 (docs: Preview without writing).
    step = scenario.run(
        "install --dry-run",
        exit=0,
        credit=[_pair(GATE=_DD, DRYRUN="on")],
        keep=_KEEP,
        says=[_WOULD_BLOCK],
    )
    assert_changes(step.changed, (), label="dry-run")
    scenario.finish()


def test_no_policy_installs_a_denied_dependency_and_says_so(lifecycle_world) -> None:
    scenario = _skill(lifecycle_world)
    scenario.model = scenario.model.with_policy("allow")
    step = scenario.run(
        "install --no-policy",
        credit=[_pair(GATE=_DD, NOPOLICY="on")],
        keep=_KEEP,
        says=[_NO_POLICY_SAYS],
    )
    assert step.result.returncode == 0, step.output
    scenario.finish()


# -- the user's bytes under a denied dependency or --no-policy ------------------------

_DEPLOYED = ("clean", "edited", "present-provenance-unknown", "unowned")
# The lockfile each deployed state leaves: a converged install, or none at all.
_LOCK_OF = {
    "clean": "consistent",
    "edited": "consistent",
    "present-provenance-unknown": "absent",
    "unowned": "absent",
}


def _kept_credit(deployed: str) -> list[dict[str, str]]:
    return [_pair(DEPLOYED=deployed, GATE=_DD), _pair(LOCK=_LOCK_OF[deployed], GATE=_DD)]


@pytest.mark.parametrize("deployed", _DEPLOYED)
def test_a_denied_dependency_keeps_the_deployed_skill(lifecycle_world, deployed) -> None:
    scenario = _skill(lifecycle_world, deployed)
    line = "install p-skill" if deployed == "unowned" else "install"
    assert scenario.lock_path.exists() == (_LOCK_OF[deployed] == "consistent")
    step = scenario.run(line, credit=_kept_credit(deployed), keep=_KEEP, refusal=True)
    assert "Nothing was changed" in " ".join(step.output.split()), step.output
    scenario.finish()


@pytest.mark.parametrize("deployed", [d for d in _DEPLOYED if d != "clean"])
def test_no_policy_keeps_the_users_skill(lifecycle_world, deployed) -> None:
    scenario = _skill(lifecycle_world, deployed)
    scenario.model = scenario.model.with_policy("allow")
    line = (
        "install --no-policy --dry-run p-skill" if deployed == "unowned" else "install --no-policy"
    )
    step = scenario.run(
        line, credit=[_pair(DEPLOYED=deployed, NOPOLICY="on")], keep=_KEEP, says=[_NO_POLICY_SAYS]
    )
    assert step.result.returncode == 0, step.output
    scenario.finish()


# -- target changes ------------------------------------------------------------------

_TARGETS = {
    "single-unchanged": (("copilot",), ("copilot",)),
    "single-narrowed": (("copilot",), ("claude",)),
    "single-widened": (("copilot",), ("copilot", "claude")),
    "multi-unchanged": (("copilot", "claude"), ("copilot", "claude")),
    "multi-narrowed": (("copilot", "claude"), ("copilot",)),
    "multi-widened": (("copilot", "claude"), ("copilot", "claude", "cursor")),
}


def _targeted(world, before: tuple[str, ...]) -> Scenario:
    use_policy(world, "allow")
    scenario = Scenario(world, ("p-skill",), policy="allow", targets=before)
    scenario.run("install p-skill")
    return scenario


@pytest.mark.parametrize("change", sorted(_TARGETS))
def test_a_target_change_under_a_denied_dependency_refuses(lifecycle_world, change) -> None:
    before, after = _TARGETS[change]
    scenario = _targeted(lifecycle_world, before)
    _deny(scenario, "p-skill")
    scenario.set_targets(*after)
    step = scenario.run("install", credit=[_pair(GATE=_DD, TGT=change)], refusal=True)
    assert "Blocked by org policy" in " ".join(step.output.split()), step.output
    scenario.finish()


@pytest.mark.parametrize("change", [c for c in sorted(_TARGETS) if not c.endswith("unchanged")])
def test_a_target_change_with_no_policy_installs_and_says_so(lifecycle_world, change) -> None:
    before, after = _TARGETS[change]
    scenario = _targeted(lifecycle_world, before)
    scenario.set_targets(*after)
    step = scenario.run(
        "install --no-policy",
        credit=[_pair(TGT=change, NOPOLICY="on")],
        says=[_NO_POLICY_SAYS],
    )
    assert re.search(r"Targets: ", step.output), step.output
    scenario.finish()


# -- each package source ---------------------------------------------------------


@pytest.mark.parametrize("src", sorted(SOURCES))
def test_a_denied_package_from_each_source_is_refused(lifecycle_world, src) -> None:
    scenario, name = from_source(lifecycle_world, src)
    variant = "deny-native" if src == "native-plugin" else _DENY
    if src in MARKETPLACES:
        # Resolve once while allowed, so the refused install finds the catalog current.
        scenario.run(f"install --dry-run {name}", **source_fields(src))
    _deny(scenario, name, variant=variant)
    step = scenario.run(
        f"install {name}",
        credit=[_pair(SRC=src, GATE=_DD)],
        refusal=True,
        **source_fields(src),
    )
    assert "Blocked by org policy" in " ".join(step.output.split()), step.output
    scenario.finish()


@pytest.mark.parametrize("src", sorted(SOURCES))
def test_no_policy_installs_from_each_source_and_says_so(lifecycle_world, src) -> None:
    scenario, name = from_source(lifecycle_world, src)
    variant = "deny-native" if src == "native-plugin" else _DENY
    use_policy(lifecycle_world, variant)
    scenario.source_moved()
    step = scenario.run(
        f"install --no-policy {name}",
        credit=[_pair(SRC=src, NOPOLICY="on")],
        says=[_NO_POLICY_SAYS],
        **source_fields(src),
    )
    assert step.result.returncode == 0, step.output
    scenario.finish()


# -- network -------------------------------------------------------------------------

_CLOSED_PROXY = "http://127.0.0.1:9"


def _offline(world) -> None:
    for name in ("HTTPS_PROXY", "https_proxy"):
        world.env[name] = _CLOSED_PROXY


def test_a_denied_git_package_is_refused_online(lifecycle_world) -> None:
    scenario, name = from_source(lifecycle_world, "git")
    _deny(scenario, name)
    lifecycle_world.services.reset()
    step = scenario.run(
        f"install {name}", credit=[_pair(GATE=_DD, NET="online", SRC="git")], refusal=True
    )
    assert step.result.returncode != 0, step.output
    assert lifecycle_world.services.egress_log(), "an online install must reach the network"
    scenario.finish()


def test_a_denied_package_is_refused_offline(lifecycle_world) -> None:
    scenario = _skill(lifecycle_world)
    # The deny policy is fetched (and cached) while online; offline installs use the cache.
    scenario.run("install --dry-run", exit=0, keep=_KEEP)
    _offline(lifecycle_world)
    lifecycle_world.services.reset()
    step = scenario.run(
        "install", credit=[_pair(GATE=_DD, NET="offline")], keep=_KEEP, refusal=True
    )
    assert step.result.returncode != 0, step.output
    assert lifecycle_world.services.egress_log() == []
    scenario.finish()


def test_no_policy_installs_offline(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, ("p-skill",), policy="allow")
    _offline(lifecycle_world)
    lifecycle_world.services.reset()
    step = scenario.run(
        "install --no-policy p-skill",
        credit=[_pair(NET="offline", NOPOLICY="on")],
        says=[_NO_POLICY_SAYS],
    )
    assert step.result.returncode == 0, step.output
    assert lifecycle_world.services.egress_log() == []
    scenario.finish()


# -- operations beside a denied dependency ------------------------------------------


def test_marketplace_add_beside_a_denied_dependency(lifecycle_world) -> None:
    scenario = _skill(lifecycle_world)
    step = scenario.run(
        f"marketplace add {lifecycle_world.services.marketplace_url}",
        credit=[_pair(OP="marketplace", GATE=_DD)],
        keep=_KEEP,
        forbidden={"project": ("*",)},
        allowed={},
    )
    assert step.result.returncode == 0, step.output
    scenario.finish()


def test_no_policy_installs_a_local_bundle(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    specs = {"p-exec-bundle": lifecycle_world.packages["p-exec:bundle"].spec}
    kinds = {"p-exec-bundle": ("hook", "mcp", "bin")}
    scenario = Scenario(lifecycle_world, specs, policy="allow", kinds=kinds)
    step = scenario.run(
        "install --no-policy p-exec-bundle",
        credit=[_pair(OP="bundle-install", SRC="local-bundle", NOPOLICY="on")],
        says=[_NO_POLICY_SAYS],
    )
    assert step.result.returncode == 0, step.output
    scenario.finish()


def test_a_denied_local_bundle_is_refused_before_any_write(lifecycle_world) -> None:
    """A local bundle meets dependencies.deny by its name and its recorded packages (#4646)."""
    world = lifecycle_world
    use_policy(world, "allow")
    specs = {"p-exec-bundle": world.packages["p-exec:bundle"].spec}
    kinds = {"p-exec-bundle": ("hook", "mcp", "bin")}
    scenario = Scenario(world, specs, policy="allow", kinds=kinds)
    _deny(scenario, "p-exec-bundle")
    # Warm the policy cache first, so the refusal itself may change no cache byte.
    warmed = driver.run(world, ["policy", "status"])
    assert warmed.returncode == 0, warmed.stdout + warmed.stderr
    roots = dict(scenario.snapshot_roots())
    cache = world.cache_root
    if cache is not None and not any(cache.is_relative_to(r) for r in roots.values()):
        roots["cache"] = cache
    before = ArtifactSnapshotSet.capture(roots)
    step = scenario.run(
        "install p-exec-bundle",
        credit=[
            _pair(OP="bundle-install", GATE=_DD),
            _pair(SRC="local-bundle", GATE=_DD),
        ],
        refusal=True,
    )
    # Project, lock, ledger, APM_HOME and cache are byte-identical after the refusal.
    assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture(roots))
    assert "denied by pattern" in " ".join(step.output.split()), step.output
    scenario.finish()


# -- MCP server sources --------------------------------------------------------------

_MCP_LINES = {
    "mcp-registry": f"install --mcp {REGISTRY_SERVER}",
    "inline-mcp": f"install --mcp p-inline -- {INERT_COMMAND} --stdio",
}


@pytest.mark.parametrize("src", sorted(_MCP_LINES))
def test_an_org_denied_mcp_server_is_refused(lifecycle_world, src) -> None:
    use_policy(lifecycle_world, "mcp-deny-all")
    scenario = Scenario(lifecycle_world, (), policy="mcp-deny-all")
    step = scenario.run(_MCP_LINES[src], exit=1, credit=[_pair(SRC=src, GATE=_DD)], refusal=True)
    assert step.result.returncode == 1, step.output
    assert "mcp.json" not in " ".join(sorted(step.changed)), step.changed
    scenario.finish()


@pytest.mark.parametrize("src", sorted(_MCP_LINES))
def test_no_policy_adds_an_mcp_server_and_says_so(lifecycle_world, src) -> None:
    use_policy(lifecycle_world, "mcp-deny-all")
    scenario = Scenario(lifecycle_world, (), policy="allow")
    line = _MCP_LINES[src].replace("install --mcp", "install --no-policy --mcp")
    step = scenario.run(line, credit=[_pair(SRC=src, NOPOLICY="on")], says=[_NO_POLICY_SAYS])
    assert step.result.returncode == 0, step.output
    scenario.finish()


# -- a plugin's bin/ (user scope only) ---------------------------------------------


# #3703: a user-scope install discovers no org policy.
_USER_SCOPE_DENY_KNOWN = Known("#3703", r"exited 0, expected \[1\]")


def test_a_denied_plugin_with_bin_is_refused_at_user_scope(lifecycle_world) -> None:
    """#3703: a user-scope install discovers no org policy, so the deny never applies."""
    use_policy(lifecycle_world, "deny-native")
    spec = lifecycle_world.packages["p-native-claude-bin:git"].spec
    scenario = Scenario(
        lifecycle_world,
        {"p-native-claude-bin": spec},
        scope="user",
        targets=("claude",),
        policy="deny-native",
    )
    scenario.model = scenario.model.with_policy("deny-native", ["p-native-claude-bin"])
    step = scenario.run(
        "install p-native-claude-bin --trust-bin",
        known={"R": _USER_SCOPE_DENY_KNOWN},
    )
    with known_defect("#3703", r"^a user-scope install ignored the org deny"):
        assert step.result.returncode == 1, "a user-scope install ignored the org deny"


CREDITS = credits(
    __file__,
    {
        **{
            f"test_a_denied_dependency_refuses_every_resolving_install[{row}]": [
                _pair(GATE=_DD, **factors)
            ]
            for row, (_line, factors) in _REFUSED_LINES.items()
        },
        "test_a_dry_run_says_what_the_policy_would_block": [_pair(GATE=_DD, DRYRUN="on")],
        "test_no_policy_installs_a_denied_dependency_and_says_so": [_pair(GATE=_DD, NOPOLICY="on")],
        **{
            f"test_a_denied_dependency_keeps_the_deployed_skill[{d}]": _kept_credit(d)
            for d in _DEPLOYED
        },
        **{
            f"test_no_policy_keeps_the_users_skill[{d}]": [_pair(DEPLOYED=d, NOPOLICY="on")]
            for d in _DEPLOYED
            if d != "clean"
        },
        **{
            f"test_a_target_change_under_a_denied_dependency_refuses[{c}]": [_pair(GATE=_DD, TGT=c)]
            for c in _TARGETS
        },
        **{
            f"test_a_target_change_with_no_policy_installs_and_says_so[{c}]": [
                _pair(TGT=c, NOPOLICY="on")
            ]
            for c in _TARGETS
            if not c.endswith("unchanged")
        },
        **{
            f"test_a_denied_package_from_each_source_is_refused[{src}]": [_pair(SRC=src, GATE=_DD)]
            for src in SOURCES
        },
        **{
            f"test_no_policy_installs_from_each_source_and_says_so[{src}]": [
                _pair(SRC=src, NOPOLICY="on")
            ]
            for src in SOURCES
        },
        "test_a_denied_git_package_is_refused_online": [_pair(GATE=_DD, NET="online", SRC="git")],
        "test_a_denied_package_is_refused_offline": [_pair(GATE=_DD, NET="offline")],
        "test_no_policy_installs_offline": [_pair(NET="offline", NOPOLICY="on")],
        "test_marketplace_add_beside_a_denied_dependency": [_pair(OP="marketplace", GATE=_DD)],
        "test_a_denied_local_bundle_is_refused_before_any_write": [
            _pair(OP="bundle-install", GATE=_DD),
            _pair(SRC="local-bundle", GATE=_DD),
        ],
        "test_no_policy_installs_a_local_bundle": [
            _pair(OP="bundle-install", SRC="local-bundle", NOPOLICY="on")
        ],
        **{
            f"test_an_org_denied_mcp_server_is_refused[{src}]": [_pair(SRC=src, GATE=_DD)]
            for src in _MCP_LINES
        },
        **{
            f"test_no_policy_adds_an_mcp_server_and_says_so[{src}]": [_pair(SRC=src, NOPOLICY="on")]
            for src in _MCP_LINES
        },
        "test_a_denied_plugin_with_bin_is_refused_at_user_scope": [
            _pair(PRIM="bin", GATE=_DD, SCOPE="user")
        ],
    },
    known={"test_a_denied_plugin_with_bin_is_refused_at_user_scope": _USER_SCOPE_DENY_KNOWN},
)
