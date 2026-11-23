"""Target history across sources, gates, MCP, bundles and marketplaces (#4100).

PROMISE: P-OWN-effective-install-target-selection -- the first command after
the user changes ``targets:`` in apm.yml reconciles every source and primitive
to the new set (source: install/service.py target resolution).
PROMISE: P-INV-I4 -- gates are monotone across a target change: a parked or
revoked executable stays undeployed on every target.

TGT follows ruling #3 (``TARGET_CHANGES`` in tests/lifecycle/generator/factors.py):
the shape is the declared set BEFORE the step; widened adds a target, narrowed
drops one (a switch counts as narrowed). Each row deploys at the old targets,
edits ``targets:``, runs ONE command through the scenario kit with org policy
on (I1-I9) and credits the scenario-lane pairs its assignment implies.
"""

from __future__ import annotations

import json

import pytest

from tests.lifecycle import driver
from tests.lifecycle.scenarios._kit import (
    PROJECT_HOME_ALLOWED,
    Scenario,
    credits,
    mcp_server_problems,
)
from tests.lifecycle.world import use_policy, use_registry
from tests.lifecycle.world.services import REGISTRY_SERVER

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I4",
        "P-INV-I5",
        "P-OWN-effective-install-target-selection",
    ),
]

# TGT value -> (declared targets before the step, after it); ruling #3.
TARGET_HISTORIES = {
    "single-unchanged": (("copilot",), ("copilot",)),
    "multi-unchanged": (("copilot", "claude"), ("copilot", "claude")),
    "single-narrowed": (("copilot",), ("claude",)),
    "single-widened": (("copilot",), ("copilot", "claude")),
    "multi-narrowed": (("copilot", "claude"), ("copilot",)),
    "multi-widened": (("copilot", "claude"), ("copilot", "claude", "gemini")),
}
# Every row converges before the edit: the lockfile matches apm.yml's dependencies.
_CONTEXT = {"SCOPE": "project", "LOCK": "consistent", "DEPLOYED": "clean"}
_CLOSED_PROXY = "http://127.0.0.1:9"
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
_INLINE = ("install", "--mcp", "p-inline", "--", "apm-lifecycle-inert-server", "--stdio")


def _go_offline(world) -> None:
    for name in _PROXY_VARS:
        world.env.pop(name, None)
    world.env["HTTPS_PROXY"] = world.env["https_proxy"] = _CLOSED_PROXY
    world.env["NO_PROXY"] = world.env["no_proxy"] = "localhost"


def _unit(op: str, tgt: str, src: str, prim: str, gate: str | None, **extra) -> dict[str, str]:
    item = {"OP": op, "TGT": tgt, "SRC": src, "PRIM": prim, **_CONTEXT, **extra}
    if gate is not None:
        item["GATE"] = gate
    return item


# -- rows -------------------------------------------------------------------------

# Approved executables of every remaining kind and the package registry, installed
# offline after the target change: SRC, PRIM and NET=offline x TGT.
_KINDS = {
    "p-lsp": (("local-path", "lsp"),),
    "p-canvas": (("local-path", "canvas"),),
    # A native plugin without bin/ deploys whole once approved (#4559); it proves its
    # skill inside the plugin root (_assert_whole_plugin).
    "p-native-claude": (("native-plugin", "skill"),),
}
_NATIVE = "p-native-claude"
# Harness -> the settings file that enables a whole-deployed plugin (#4559).
_PLUGIN_SETTINGS = {"claude": ".claude/settings.json", "copilot": ".github/copilot/settings.json"}


def _assert_whole_plugin(scenario: Scenario, targets: tuple[str, ...], output: str) -> None:
    """The native plugin sits whole under .agents/plugins and every current harness
    that loads plugins enables it."""
    skill = scenario.deploy_root / f".agents/plugins/{_NATIVE}/skills/{_NATIVE}/SKILL.md"
    assert skill.is_file(), f"{_NATIVE} is not deployed whole\n{output}"
    for target in sorted(set(targets) & set(_PLUGIN_SETTINGS)):
        settings = scenario.deploy_root / _PLUGIN_SETTINGS[target]
        enabled = json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins") or {}
        mine = {key: on for key, on in enabled.items() if key.split("@")[0] == _NATIVE}
        assert list(mine.values()) == [True], f"{target}: {enabled}\n{output}"


_REGISTRY = "p-skill:registry"


def _kinds_credit(tgt: str) -> list[dict[str, str]]:
    found = [
        _unit("install", tgt, src, prim, "allowed", NET="offline")
        for units in _KINDS.values()
        for src, prim in units
    ]
    found.append(_unit("install", tgt, "pkg-registry", "skill", "none", NET="offline"))
    return found


def _gates_credit(tgt: str) -> list[dict[str, str]]:
    found = [
        _unit("install", tgt, "local-path", prim, "unapproved-exec", NET="online")
        for prim in ("hook", "mcp")
    ]
    found.append(
        _unit("install", tgt, "local-path", "canvas", "revoked-after-deploy", NET="online")
    )
    return found


def _mcp_credit(tgt: str, src: str) -> list[dict[str, str]]:
    return [_unit("mcp-install", tgt, src, "mcp", None, NET="online")]


def _bundle_credit(tgt: str) -> list[dict[str, str]]:
    return [
        _unit("bundle-install", tgt, "local-bundle", prim, "unapproved-exec", NET="online")
        for prim in ("hook", "mcp")
    ]


# SRC follows the registration source (marketplace/registration.py): a git URL, a
# local directory holding the catalog, or the path of the catalog file itself.
_MARKETPLACES = {
    "marketplace-git": ("lifecycle-market", "p-skill"),
    "marketplace-local": ("lifecycle-local", "p-lsp"),
    "marketplace-file": ("lifecycle-file", "p-exec"),
}
_MARKET_UNITS = {
    "p-skill": (("skill", "none"),),
    "p-lsp": (("lsp", "unapproved-exec"),),
    "p-exec": (("hook", "unapproved-exec"), ("mcp", "unapproved-exec")),
}
_CATALOG_CACHE = (
    ".apm/cache/marketplace/version-pins.json",
    *(
        f".apm/cache/marketplace/{name}{suffix}"
        for name, _package in _MARKETPLACES.values()
        for suffix in (".json", ".meta.json")
    ),
)


def _market_credit(op: str, tgt: str) -> list[dict[str, str]]:
    return [
        _unit(op, tgt, src, prim, gate, NET="online")
        for src, (_name, package) in _MARKETPLACES.items()
        for prim, gate in _MARKET_UNITS[package]
    ]


def _register_marketplaces(world) -> dict[str, str]:
    """Register the three catalogs; return package name -> ``name@marketplace``."""
    root = world.root / "catalogs"
    for src, (name, package) in _MARKETPLACES.items():
        if src == "marketplace-git":
            source = world.services.marketplace_url
        else:
            plugin = {
                "name": package,
                "description": f"Lifecycle world package {package}",
                "source": {"source": "github", "repo": f"lifecycle/{package}", "ref": "v1.0.0"},
            }
            document = {"name": name, "owner": {"name": "APM Lifecycle"}, "plugins": [plugin]}
            catalog = root / name / ".claude-plugin" / "marketplace.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")
            source = str(catalog if src == "marketplace-file" else root / name)
        result = driver.run(world, ("marketplace", "add", source), scope="project")
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    return {package: f"{package}@{name}" for name, package in _MARKETPLACES.values()}


_BUNDLE_KINDS = ("hook", "mcp", "bin")
# The server each MCP row requests, by every name it may be keyed under.
_MCP_NAMES = {
    "mcp-registry": frozenset({REGISTRY_SERVER, REGISTRY_SERVER.rsplit("/", 1)[-1]}),
    "inline-mcp": frozenset({"p-inline"}),
}

# install --mcp, install <bundle> and marketplace list are side doors: they do not
# reconcile the declared packages to the new targets; the next install does.
_RECONCILING_ROWS = frozenset({"kinds-offline", "gates", "marketplace-install"})
ROWS = (
    "kinds-offline",
    "gates",
    "mcp-registry",
    "inline-mcp",
    "bundle",
    "marketplace-install",
    "marketplace-list",
)


def _credit(row: str, tgt: str) -> list[dict[str, str]]:
    if row == "kinds-offline":
        return _kinds_credit(tgt)
    if row == "gates":
        return _gates_credit(tgt)
    if row in ("mcp-registry", "inline-mcp"):
        return _mcp_credit(tgt, row)
    if row == "bundle":
        return _bundle_credit(tgt)
    return _market_credit("install" if row == "marketplace-install" else "marketplace", tgt)


def _deploy(world, row: str, before) -> tuple[Scenario, object, dict]:
    """Deploy the row's project at the old targets; return (scenario, command, run fields)."""
    use_policy(world, "allow")
    if row == "kinds-offline":
        use_registry(world)
        names = (*_KINDS, _REGISTRY)
        scenario = Scenario(world, names, targets=before, policy="allow")
        scenario.run(f"install {' '.join(names)}")
        scenario.run(f"approve {' '.join(_KINDS)}")
        installed = scenario.run("install")
        _assert_whole_plugin(scenario, before, installed.output)
        return scenario, "install", {}
    if row == "gates":
        names = ("p-exec", "p-canvas")
        scenario = Scenario(world, names, targets=before, policy="allow")
        scenario.run(f"install {' '.join(names)}")
        scenario.run("approve p-canvas")
        scenario.run("install")
        scenario.run("deny p-canvas")
        return scenario, "install", {}
    if row in ("marketplace-install", "marketplace-list"):
        specs = _register_marketplaces(world)
        scenario = Scenario(world, specs, targets=before, policy="allow")
        home = {"home": (*PROJECT_HOME_ALLOWED, *_CATALOG_CACHE)}
        scenario.run(f"install {' '.join(specs)}", allowed=home)
        if row == "marketplace-list":
            return scenario, "marketplace list", {"read_only": True}
        return scenario, "install", {"allowed": home}
    specs = {"p-skill:git": world.packages["p-skill:git"].spec}
    kinds = {"p-skill:git": ("skill",)}
    if row == "bundle":
        # The bundle packs p-exec: model it, so I4 observes its hook and MCP server.
        specs["p-exec"] = world.packages["p-exec:bundle"].spec
        kinds["p-exec"] = _BUNDLE_KINDS
    scenario = Scenario(world, specs, kinds=kinds, targets=before, policy="allow")
    scenario.run("install p-skill:git")
    if row == "mcp-registry":
        return scenario, ("install", "--mcp", REGISTRY_SERVER), {}
    if row == "inline-mcp":
        return scenario, _INLINE, {}
    return scenario, ("install", specs["p-exec"]), {}


# Every row runs after each target change; the unchanged shapes run only for the
# marketplace and offline rows, whose unchanged-TGT pairs no other row reaches.
_CHANGED = ("single-narrowed", "single-widened", "multi-narrowed", "multi-widened")
CASES = (
    *((row, tgt) for row in ROWS for tgt in _CHANGED),
    *(
        (row, tgt)
        for row in ("kinds-offline", "marketplace-install")
        for tgt in ("single-unchanged", "multi-unchanged")
    ),
)


@pytest.mark.parametrize(("row", "tgt"), CASES, ids=[f"{row}-{tgt}" for row, tgt in CASES])
def test_first_command_after_a_target_change(lifecycle_world, row, tgt) -> None:
    before, after = TARGET_HISTORIES[tgt]
    scenario, command, fields = _deploy(lifecycle_world, row, before)
    scenario.set_targets(*after)
    if row == "kinds-offline":
        _go_offline(lifecycle_world)
    step = scenario.run(command, credit=_credit(row, tgt), **fields)
    assert step.result.returncode == 0, step.output
    if row in _RECONCILING_ROWS:
        scenario.assert_deployed()
    if row == "kinds-offline":
        _assert_whole_plugin(scenario, after, step.output)
    if row in _MCP_NAMES:
        problems = mcp_server_problems(scenario, _MCP_NAMES[row])
        assert not problems, f"{problems}\n{step.output}"
    if row == "bundle":
        observed, _, _ = scenario.deployed()
        parked = {cell for cell in observed if cell.startswith("p-exec/")}
        assert not parked, f"unapproved bundle executables deployed: {sorted(parked)}"
    scenario.finish()


_CLAUDE_LSP = ".claude/skills/apm-lsp/.claude-plugin/plugin.json"


_HOOK = {"hooks": [{"type": "command", "command": "echo p-exec"}]}
_SETTINGS = ".claude/settings.json"
_SIDECAR = ".claude/apm-hooks.json"


def _owned(source: str) -> dict:
    return {"SessionStart": [{**_HOOK, "_apm_source": source}]}


# (cell, the files written, whether Claude has the package's entry).
_CLAUDE_CONTROLS = (
    ("p-lsp/lsp/claude", {_CLAUDE_LSP: {"description": "p-lsp-server", "lspServers": {}}}, False),
    ("p-lsp/lsp/claude", {_CLAUDE_LSP: {"lspServers": {"p-lsp-server": {"command": "x"}}}}, True),
    ("p-exec/mcp/claude", {".mcp.json": {"note": "p-exec-mcp", "mcpServers": {}}}, False),
    ("p-exec/mcp/claude", {".mcp.json": {"mcpServers": {"p-exec-mcp": {"command": "x"}}}}, True),
    # A sidecar alone, or one owning an entry Claude does not have, is no hook.
    ("p-exec/hook/claude", {_SIDECAR: _owned("_local/p-exec")}, False),
    ("p-exec/hook/claude", {_SETTINGS: {"hooks": {}}, _SIDECAR: _owned("_local/p-exec")}, False),
    (
        "p-exec/hook/claude",
        {
            _SETTINGS: {"hooks": {"SessionStart": [{"hooks": [{"command": "echo other"}]}]}},
            _SIDECAR: _owned("_local/p-exec"),
        },
        False,
    ),
    # Registered and owned: by the dependency, or by the local bundle (#4027 marker).
    (
        "p-exec/hook/claude",
        {_SETTINGS: {"hooks": {"SessionStart": [_HOOK]}}, _SIDECAR: _owned("_local/p-exec")},
        True,
    ),
    (
        "p-exec/hook/claude",
        {
            _SETTINGS: {"hooks": {"SessionStart": [_HOOK]}},
            _SIDECAR: _owned("local-bundle::p-exec::sha256:abc"),
        },
        True,
    ),
)


def test_claude_surfaces_count_only_their_own_entries(lifecycle_world) -> None:
    """Negative controls: Claude's shared LSP, MCP and hook files count for a package
    only when its entry sits where Claude reads it, never on a bare mention or a
    sidecar without the registered hook."""
    scenario = Scenario(lifecycle_world, ("p-lsp", "p-exec"), targets=("claude",))
    for cell, files, counted in _CLAUDE_CONTROLS:
        for relative in (_CLAUDE_LSP, ".mcp.json", _SETTINGS, _SIDECAR):
            (lifecycle_world.project / relative).unlink(missing_ok=True)
        for relative, document in files.items():
            path = lifecycle_world.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document), encoding="ascii")
        observed, _, _ = scenario.deployed()
        written = {r: (lifecycle_world.project / r).read_text(encoding="ascii") for r in files}
        assert written and (cell in observed) == counted, (cell, written, sorted(observed))


def test_a_dropped_target_that_still_enables_a_whole_plugin_keeps_it(lifecycle_world) -> None:
    """Negative control: the plugin root is shared by claude and copilot, but each
    harness's enable entry is its own, so a dropped claude still enabling it counts."""
    scenario = Scenario(lifecycle_world, (_NATIVE,), targets=("copilot",))
    root = lifecycle_world.project / f".agents/plugins/{_NATIVE}/.claude-plugin/plugin.json"
    root.parent.mkdir(parents=True)
    root.write_text(json.dumps({"name": _NATIVE}), encoding="ascii")
    settings = lifecycle_world.project / _PLUGIN_SETTINGS["claude"]
    settings.parent.mkdir(parents=True)
    cell = f"{_NATIVE}/native/claude"
    for enabled in (True, False):
        document = {"enabledPlugins": {f"{_NATIVE}@apm-p-0123abcd": enabled}}
        settings.write_text(json.dumps(document), encoding="ascii")
        written = settings.read_text(encoding="ascii")
        kept = scenario.undeclared_target_cells()
        assert written and (cell in kept) == enabled, (written, sorted(kept))


CREDITS = credits(
    __file__,
    {
        f"test_first_command_after_a_target_change[{row}-{tgt}]": _credit(row, tgt)
        for row, tgt in CASES
    },
)
