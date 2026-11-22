"""User scope (``-g``) across the lifecycle factors (#4097).

PROMISE: P-INV-I5 -- a user-scope command writes only under HOME and APM_HOME
and never touches the project (source: tests/lifecycle/scenarios/_kit.py
Scenario.operation, docs/src/content/docs/guides/user-scope.md).
PROMISE: P-INV-I1 -- the user lockfile (~/.apm/apm.lock.yaml) matches what is
deployed under HOME after every user-scope command.

T-3 crosses SCOPE=user with the source, primitive, target, deployed-state,
network and modifier factors. Every row installs at user scope and applies
I1-I9 through the scenario kit; its credit names the factors the step proves.
A first install proves DEPLOYED=absent (nothing of the package is deployed
before it, tests/lifecycle/generator/covering.py).
"""

from __future__ import annotations

import json

import pytest

from tests.lifecycle.scenarios._kit import (
    Known,
    Scenario,
    assert_changes,
    credits,
    known_defect,
    mcp_server_problems,
)
from tests.lifecycle.scenarios._sources import SOURCES, from_source
from tests.lifecycle.scenarios.test_network_offline import go_offline
from tests.lifecycle.world import use_policy
from tests.lifecycle.world.packages import INERT_COMMAND
from tests.lifecycle.world.services import REGISTRY_SERVER

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I1", "P-INV-I5"),
]

_USER = {"SCOPE": "user"}
_FIRST = {**_USER, "DEPLOYED": "absent", "NET": "online", "TGT": "single-unchanged"}
_SKILL = ".agents/skills/p-skill/SKILL.md"
_NO_POLICY_SAYS = r"Policy enforcement disabled by --no-policy for this invocation"


def _user(world, *packages: str, targets=("copilot",)) -> Scenario:
    use_policy(world, "allow")
    return Scenario(world, packages, scope="user", targets=targets, policy="allow")


# -- each source -------------------------------------------------------------------

# Every source serves p-skill, except a native plugin: Claude registers it
# (~/.claude/settings.json); it deploys no primitive file.
_SOURCE_ROWS = {**{src: SOURCES[src] for src in SOURCES}, "local-path": ("p-skill", "p-skill")}


def _source_credit(src: str) -> dict[str, str]:
    prim = {} if src == "native-plugin" else {"PRIM": "skill"}
    return {**_FIRST, "OP": "install", "SRC": src, **prim}


@pytest.mark.parametrize("src", sorted(_SOURCE_ROWS))
def test_a_first_install_from_each_source_deploys_under_home(lifecycle_world, src) -> None:
    if src == "local-path":
        scenario, name = _user(lifecycle_world, "p-skill"), "p-skill"
    else:
        scenario, name = from_source(lifecycle_world, src, scope="user")
    if src == "native-plugin":
        # The plugin ships an MCP server, so the user-scope gate parks the whole
        # plugin until it is approved; the approved install is the source credit.
        scenario.run(f"install {name}")
        scenario.run(f"approve {name}")
        step = scenario.run("install", credit=[_source_credit(src)])
    else:
        step = scenario.run(f"install {name}", credit=[_source_credit(src)])
    scenario.assert_deployed()
    assert not any(item.startswith("project:") for item in step.changed), step.changed
    if src != "native-plugin":
        assert (lifecycle_world.home / _SKILL).is_file(), step.output
    if src == "native-plugin":
        assert scenario.lock_names(), "the user lock records the native plugin"
        path = lifecycle_world.home / ".claude/settings.json"
        claude = sorted(
            entry.relative_to(lifecycle_world.home).as_posix()
            for entry in (lifecycle_world.home / ".claude").rglob("*")
        )
        assert path.is_file(), f"Claude settings not written; ~/.claude: {claude}\n{step.output}"
        settings = json.loads(path.read_text(encoding="utf-8"))
        enabled = {
            key.split("@")[0]
            for key, on in (settings.get("enabledPlugins") or {}).items()
            if on is True
        }
        assert name in enabled, f"{name} is not enabled for Claude: {settings}"
    if src == "pkg-registry":
        sources = {dep.source for dep in scenario.lock().get_package_dependencies()}
        assert sources == {"registry"}, f"SRC=pkg-registry resolved through {sources}"
    scenario.finish()


_MCP_LINES = {
    "mcp-registry": (f"install --mcp {REGISTRY_SERVER}", REGISTRY_SERVER.rsplit("/", 1)[-1]),
    "inline-mcp": (f"install --mcp p-inline -- {INERT_COMMAND} --stdio", "p-inline"),
}
_COPILOT_MCP = ".copilot/mcp-config.json"


@pytest.mark.parametrize("src", sorted(_MCP_LINES))
def test_install_mcp_configures_a_user_scope_server(lifecycle_world, src) -> None:
    scenario = _user(lifecycle_world)
    line, server = _MCP_LINES[src]
    credit = {**_FIRST, "OP": "mcp-install", "SRC": src, "PRIM": "mcp"}
    step = scenario.run(line, credit=[credit])
    assert step.result.returncode == 0, step.output
    config = json.loads((lifecycle_world.home / _COPILOT_MCP).read_text(encoding="utf-8"))
    assert server in (config.get("mcpServers") or {}), config
    names = {server, REGISTRY_SERVER} if src == "mcp-registry" else {server}
    problems = mcp_server_problems(scenario, names)
    assert not problems, f"{problems}\n{step.output}"
    assert not any(item.startswith("project:") for item in step.changed), step.changed
    scenario.finish()


# -- each executable primitive -----------------------------------------------------

_EXEC_PRIMS = ("hook", "lsp", "mcp")


def test_approved_executables_deploy_under_home(lifecycle_world) -> None:
    scenario = _user(lifecycle_world, "p-exec", "p-lsp")
    scenario.run("install p-exec p-lsp")
    scenario.run("approve p-exec p-lsp")
    credit = [{**_FIRST, "OP": "install", "PRIM": p, "GATE": "allowed"} for p in _EXEC_PRIMS]
    scenario.run("install", credit=credit)
    scenario.assert_deployed()
    scenario.finish()


def test_an_approved_canvas_deploys_under_home(lifecycle_world) -> None:
    scenario = _user(lifecycle_world, "p-canvas")
    scenario.run("install p-canvas")
    scenario.run("approve p-canvas")
    credit = {**_FIRST, "OP": "install", "PRIM": "canvas", "GATE": "allowed"}
    step = scenario.run("install", credit=[credit])
    scenario.assert_deployed()
    assert (lifecycle_world.home / ".copilot/extensions/p-canvas/extension.mjs").is_file()
    assert "reload the Copilot session" in step.output, step.output
    scenario.finish()


_PLUGIN_BIN = ".agents/plugins/p-native-claude-bin/bin/p-native-claude-bin"


def test_a_plugin_bin_deploys_under_home_once_trusted(lifecycle_world) -> None:
    """U-BIN: bin/ deploys only for a Claude plugin at user scope (exec_gate.plugin_bin_deployable)."""
    use_policy(lifecycle_world, "allow")
    specs = {"p-native-claude-bin": lifecycle_world.packages["p-native-claude-bin:git"].spec}
    scenario = Scenario(lifecycle_world, specs, scope="user", targets=("claude",), policy="allow")
    scenario.run("install p-native-claude-bin")
    scenario.run("approve p-native-claude-bin")
    credit = {**_FIRST, "OP": "install", "PRIM": "bin", "GATE": "allowed", "SRC": "git"}
    scenario.run("install --trust-bin", credit=[credit])
    assert (lifecycle_world.home / _PLUGIN_BIN).is_file()
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


@pytest.mark.parametrize("change", sorted(_TARGETS))
def test_a_target_change_at_user_scope_redeploys_under_home(lifecycle_world, change) -> None:
    before, after = _TARGETS[change]
    scenario = _user(lifecycle_world, "p-skill", targets=before)
    scenario.run("install p-skill")
    scenario.set_targets(*after)
    step = scenario.run("install", credit=[{**_USER, "TGT": change, "PRIM": "skill"}])
    scenario.assert_deployed()
    assert not any(item.startswith("project:") for item in step.changed), step.changed
    scenario.finish()


_CHANGED = ("single-narrowed", "single-widened", "multi-narrowed", "multi-widened")
# #4744: dropping claude from targets: leaves the plugin bin deployed.
_BIN_KNOWN = {
    "multi-narrowed": Known("#4744", r"^a dropped claude target left the plugin bin"),
}


def _bin_credit(change: str) -> dict[str, str]:
    return {**_USER, "OP": "install", "PRIM": "bin", "GATE": "allowed", "SRC": "git", "TGT": change}


@pytest.mark.parametrize("change", _CHANGED)
def test_a_target_change_keeps_a_plugin_bin_only_while_claude_is_declared(
    lifecycle_world, change
) -> None:
    """U-BIN: plugin bin/ deploys only for a Claude target, so it follows claude in targets:."""
    before, after = _TARGETS[change]
    use_policy(lifecycle_world, "allow")
    specs = {"p-native-claude-bin": lifecycle_world.packages["p-native-claude-bin:git"].spec}
    scenario = Scenario(lifecycle_world, specs, scope="user", targets=before, policy="allow")
    scenario.run("install p-native-claude-bin")
    scenario.run("approve p-native-claude-bin")
    scenario.run("install --trust-bin")
    assert (lifecycle_world.home / _PLUGIN_BIN).is_file() == ("claude" in before)
    scenario.set_targets(*after)
    step = scenario.run("install --trust-bin", credit=[_bin_credit(change)])
    assert step.result.returncode == 0, step.output
    deployed = (lifecycle_world.home / _PLUGIN_BIN).is_file()
    if change in _BIN_KNOWN:
        known = _BIN_KNOWN[change]
        with known_defect(known.tracking, known.match):
            assert not deployed, f"a dropped claude target left the plugin bin\n{step.output}"
    else:
        assert deployed == ("claude" in after), step.output
    scenario.finish()


# -- modifiers and the network -------------------------------------------------------

_MODIFIER_LINES = {
    "force": ("install --force", {"FORCE": "on"}, ()),
    "update": ("install --update", {"UPDATE": "on"}, ()),
    "no-policy": ("install --no-policy", {"NOPOLICY": "on"}, (_NO_POLICY_SAYS,)),
}


@pytest.mark.parametrize("row", sorted(_MODIFIER_LINES))
def test_install_modifiers_at_user_scope(lifecycle_world, row) -> None:
    scenario = _user(lifecycle_world, "p-skill")
    scenario.run("install p-skill")
    line, factors, says = _MODIFIER_LINES[row]
    scenario.run(line, credit=[{**_USER, **factors}], says=says)
    scenario.assert_deployed()
    scenario.finish()


def test_a_dry_run_at_user_scope_writes_nothing(lifecycle_world) -> None:
    scenario = _user(lifecycle_world, "p-skill")
    step = scenario.run(
        "install --dry-run p-skill",
        credit=[{**_USER, "DRYRUN": "on"}],
    )
    assert_changes(step.changed, (), label="dry-run")
    scenario.finish()


def test_a_converged_user_scope_install_works_offline(lifecycle_world) -> None:
    scenario = _user(lifecycle_world, "p-skill")
    scenario.run("install p-skill")
    go_offline(lifecycle_world)
    scenario.run("install", credit=[{**_USER, "NET": "offline", "DEPLOYED": "clean"}])
    scenario.assert_deployed()
    scenario.finish()


# -- deployed states -------------------------------------------------------------------

_USER_SKILL = "---\nname: p-skill\ndescription: The user's own p-skill.\n---\n\nMine.\n"


@pytest.mark.parametrize("deployed", ("edited", "present-provenance-unknown", "unowned"))
def test_install_keeps_the_users_skill_under_home(lifecycle_world, deployed) -> None:
    scenario = _user(lifecycle_world, "p-skill")
    text = _USER_SKILL
    if deployed == "unowned":
        scenario.user_write(_SKILL, text)
        line = "install --dry-run p-skill"
    else:
        scenario.run("install p-skill")
        text = (lifecycle_world.home / _SKILL).read_text(encoding="ascii")
        if deployed == "edited":
            text += "\nMy own note.\n"
        scenario.user_write(_SKILL, text)
        if deployed == "present-provenance-unknown":
            scenario.lock_path.unlink()
            scenario.model = scenario.model.with_lock("absent")
        line = "install"
    step = scenario.run(line, credit=[{**_USER, "DEPLOYED": deployed}], keep={"home": (_SKILL,)})
    assert step.result.returncode == 0, step.output
    assert (lifecycle_world.home / _SKILL).read_text(encoding="ascii") == text
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        **{
            f"test_a_first_install_from_each_source_deploys_under_home[{src}]": [
                _source_credit(src)
            ]
            for src in _SOURCE_ROWS
        },
        **{
            f"test_install_mcp_configures_a_user_scope_server[{src}]": [
                {**_FIRST, "OP": "mcp-install", "SRC": src, "PRIM": "mcp"}
            ]
            for src in _MCP_LINES
        },
        "test_approved_executables_deploy_under_home": [
            {**_FIRST, "OP": "install", "PRIM": p, "GATE": "allowed"} for p in _EXEC_PRIMS
        ],
        "test_an_approved_canvas_deploys_under_home": [
            {**_FIRST, "OP": "install", "PRIM": "canvas", "GATE": "allowed"}
        ],
        "test_a_plugin_bin_deploys_under_home_once_trusted": [
            {**_FIRST, "OP": "install", "PRIM": "bin", "GATE": "allowed", "SRC": "git"}
        ],
        **{
            f"test_a_target_change_at_user_scope_redeploys_under_home[{change}]": [
                {**_USER, "TGT": change, "PRIM": "skill"}
            ]
            for change in _TARGETS
        },
        **{
            f"test_a_target_change_keeps_a_plugin_bin_only_while_claude_is_declared[{change}]": [
                _bin_credit(change)
            ]
            for change in _CHANGED
        },
        **{
            f"test_install_modifiers_at_user_scope[{row}]": [{**_USER, **factors}]
            for row, (_line, factors, _says) in _MODIFIER_LINES.items()
        },
        "test_a_dry_run_at_user_scope_writes_nothing": [{**_USER, "DRYRUN": "on"}],
        "test_a_converged_user_scope_install_works_offline": [
            {**_USER, "NET": "offline", "DEPLOYED": "clean"}
        ],
        **{
            f"test_install_keeps_the_users_skill_under_home[{deployed}]": [
                {**_USER, "DEPLOYED": deployed}
            ]
            for deployed in ("edited", "present-provenance-unknown", "unowned")
        },
    },
    known={
        f"test_a_target_change_keeps_a_plugin_bin_only_while_claude_is_declared[{change}]": known
        for change, known in _BIN_KNOWN.items()
    },
)
