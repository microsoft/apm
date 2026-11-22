"""Ownership: unmanaged and sibling assets survive every removal path (#4101).

PROMISE: P-LAW-ownership.preserve_unowned -- APM removes only what it
deployed; user files and other packages' files beside a removed package
survive byte for byte (source: tests/fixtures/lifecycle_bug_ledger/).
PROMISE: P-OWN-file-level-deploy-sync-cleanup -- a stale skill directory left
only for user content is released, not deleted (source: integration/base_integrator.py).
PROMISE: P-OWN-orphan-package-selection -- uninstall, prune and install
cleanup remove only orphaned packages (source: install/orphans.py).
PROMISE: P-OWN-target-scoped-deployed-file-contraction -- narrowing targets
removes only the dropped target's deployed files (source: install/manifest_reconcile.py).
PROMISE: P-OWN-legacy-mcp-ownership-key-migration -- MCP removal deletes only
the exact keys APM wrote (source: install/mcp/ownership.py).

Each row seeds unmanaged assets next to a victim package and a sibling
package, drives one removal path through ``driver.run`` with org policy on,
applies I1-I9 after every step and pins the exact changed set of the removal.
"""

from __future__ import annotations

import json
import shutil

import pytest

from tests.lifecycle.scenarios._kit import (
    Scenario,
    Tree,
    assert_changes,
    serve_repository,
)
from tests.lifecycle.world import use_policy
from tests.lifecycle.world.packages import LSP_SERVER
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I4",
        "P-INV-I5",
        "P-LAW-ownership.preserve_unowned",
        "P-OWN-file-level-deploy-sync-cleanup",
        "P-OWN-orphan-package-selection",
        "P-OWN-target-scoped-deployed-file-contraction",
        "P-OWN-legacy-mcp-ownership-key-migration",
    ),
]

USER_MCP = "user-mcp"
_USER_SKILL = "---\nname: user-own\ndescription: The user's own skill.\n---\n\nMine.\n"


def _unmanaged(project) -> Tree:
    """User files a removal must never touch, seeded before any install."""
    tree = Tree(project)
    tree.write(".agents/skills/user-own/SKILL.md", _USER_SKILL)
    tree.write(".claude/skills/user-own/SKILL.md", _USER_SKILL)
    tree.write(".github/hooks/user-hook.json", '{"hooks": {}}\n')
    tree.write(".github/instructions/user.instructions.md", "# Mine\n")
    servers = {"mcpServers": {USER_MCP: {"command": "user-server", "args": []}}}
    (project / ".mcp.json").write_text(json.dumps(servers, indent=2) + "\n", encoding="ascii")
    return tree


def _mcp_servers(project) -> set[str]:
    path = project / ".mcp.json"
    return set(json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}))


def _start(world, packages, **kwargs) -> tuple[Scenario, Tree]:
    use_policy(world, "allow")
    tree = _unmanaged(world.project)
    return Scenario(world, packages, policy="allow", **kwargs), tree


def _note(scenario: Scenario, tree: Tree, package: str) -> None:
    """A user note inside a managed skill directory, written after its deploy."""
    assert (scenario.world.project / f".agents/skills/{package}/SKILL.md").is_file()
    tree.write(f".agents/skills/{package}/NOTES.md", "my notes\n")


def _survives(scenario: Scenario, tree: Tree, label: str, *mcp: str) -> None:
    tree.assert_intact(label)
    servers = _mcp_servers(scenario.world.project)
    assert {USER_MCP, *mcp} <= servers, f"{label}: MCP entries lost: {sorted(servers)}"
    scenario.assert_deployed()
    scenario.finish()


@pytest.mark.parametrize("note", [False, True], ids=["clean-skill-dir", "user-note-in-skill-dir"])
def test_uninstall_keeps_unmanaged_and_sibling(lifecycle_world, note) -> None:
    scenario, tree = _start(lifecycle_world, ("p-skill", "p-exec"))
    scenario.run("install p-skill p-exec")
    scenario.run("approve p-exec")
    scenario.run("install")
    if note:
        _note(scenario, tree, "p-skill")

    # A user note in the skill directory is kept; the uninstall still succeeds.
    step = scenario.run("uninstall p-skill")
    assert_changes(
        step.changed,
        (
            "project:apm.yml",
            "project:apm.lock.yaml",
            "project:apm_modules/_local/p-skill*",
            "project:.agents/skills/p-skill/SKILL.md",
        ),
        label="uninstall p-skill",
    )
    tree.assert_intact("uninstall")
    _survives(scenario, tree, "uninstall", "p-exec-mcp")
    scenario.finish()


def test_prune_keeps_unmanaged_and_sibling(lifecycle_world) -> None:
    scenario, tree = _start(lifecycle_world, ("p-skill", "p-exec"))
    scenario.run("install p-skill p-exec")
    scenario.run("approve p-exec")
    scenario.run("install")
    _note(scenario, tree, "p-skill")
    scenario.drop_from_manifest("p-skill")

    step = scenario.run("prune")
    assert_changes(
        step.changed,
        (
            "project:apm.lock.yaml",
            "project:apm_modules/_local/p-skill*",
            "project:.agents/skills/p-skill/SKILL.md",
        ),
        label="prune",
    )
    _survives(scenario, tree, "prune", "p-exec-mcp")


def test_update_keeps_unmanaged_and_sibling(lifecycle_world) -> None:
    """``lifecycle/o-multi`` v1.1.0 drops skill o-gone; update retires only it."""
    repos = LocalGitRepositoryFactory(lifecycle_world.root / "repos", env=lifecycle_world.env)
    factory = LocalPackageFactory(lifecycle_world.root / "owned")
    multi = factory.create("o-multi", version="1.0.0")
    for name in ("o-multi", "o-gone"):
        body = f"---\nname: {name}\ndescription: Scenario skill {name}.\n---\n\nUse {name}.\n"
        factory.add_skill(multi, name, body)
    repository = repos.create("o-multi", source_tree=multi.root)
    repos.tag(repository, "v1.0.0", repos.commit(repository, message="o-multi v1.0.0"))
    serve_repository(lifecycle_world, "lifecycle/o-multi", repository.origin)
    specs = {
        "o-multi": "lifecycle/o-multi#^1.0.0",
        "p-skill": lifecycle_world.packages["p-skill"].spec,
    }
    scenario, tree = _start(lifecycle_world, specs, kinds={"o-multi": ("skill",)})
    scenario.run("install o-multi p-skill")
    _note(scenario, tree, "o-multi")
    gone = lifecycle_world.project / ".agents/skills/o-gone/SKILL.md"
    assert gone.is_file()
    shutil.rmtree(repository.worktree / "skills" / "o-gone")
    repos.tag(repository, "v1.1.0", repos.commit(repository, message="o-multi v1.1.0"))
    scenario.source_moved()

    step = scenario.run("update --yes")
    assert_changes(
        step.changed,
        (
            "project:apm.lock.yaml",
            "project:apm_modules/lifecycle/o-multi*",
            "project:.agents/skills/o-gone*",
        ),
        label="update drops o-gone",
    )
    assert not gone.exists()
    _survives(scenario, tree, "update")


def test_target_narrow_keeps_unmanaged_and_sibling(lifecycle_world) -> None:
    scenario, tree = _start(lifecycle_world, ("p-skill", "p-child"), targets=("copilot", "claude"))
    scenario.run("install p-skill p-child")
    scenario.assert_deployed()

    scenario.set_targets("copilot")
    step = scenario.run("install")
    assert_changes(
        step.changed,
        (
            "project:apm.lock.yaml",
            "project:.claude/skills/p-skill*",
            "project:.claude/skills/p-child*",
        ),
        label="narrow to copilot",
    )
    _survives(scenario, tree, "target narrow")


_PLUGIN_ROOT = ".agents/plugins/p-native-claude-bin"
_PLUGIN_BIN = f"{_PLUGIN_ROOT}/bin/p-native-claude-bin"


# How the first commands after dropping claude reach the lock: a full install; `apm
# lock` (keeps the plugin, it touches no deployed state) then install; a partial
# install of another package, which merges the untouched plugin's row (#4744).
_NARROW_PATHS = {
    "install": ((), "install --trust-bin", ()),
    "lock-then-install": (("lock",), "install --trust-bin", ()),
    "partial-install": (
        (),
        "install --trust-bin p-skill",
        ("home:.apm/apm.yml", "home:.apm/apm_modules/*", "home:.agents/skills/p-skill*"),
    ),
}


@pytest.mark.parametrize("path", sorted(_NARROW_PATHS))
def test_target_narrow_retires_a_user_scope_plugin_bin(lifecycle_world, path) -> None:
    """#4744: dropping claude retires the plugin bin/ only claude received; the rest stays."""
    use_policy(lifecycle_world, "allow")
    specs = {
        "p-native-claude-bin": lifecycle_world.packages["p-native-claude-bin:git"].spec,
        "p-skill": lifecycle_world.packages["p-skill"].spec,
    }
    scenario = Scenario(
        lifecycle_world, specs, scope="user", targets=("copilot", "claude"), policy="allow"
    )
    scenario.run("install p-native-claude-bin")
    scenario.run("approve p-native-claude-bin")
    scenario.run("install --trust-bin")
    home = lifecycle_world.home
    assert (home / _PLUGIN_BIN).is_file()
    kept = sorted(
        item.relative_to(home).as_posix()
        for item in (home / ".agents/skills").rglob("*")
        if item.is_file()
    )

    scenario.set_targets("copilot")
    before, line, extra = _NARROW_PATHS[path]
    for command in before:
        scenario.run(command)
        assert (home / _PLUGIN_BIN).is_file(), f"{command} touched deployed state"
        lock = scenario.lock_path.read_text(encoding="utf-8")
        assert _PLUGIN_ROOT in lock, f"{command} dropped the ownership of a deployed plugin"
    step = scenario.run(line)
    assert not (home / _PLUGIN_BIN).exists(), f"dropping claude left the plugin bin\n{step.output}"
    lock = scenario.lock_path.read_text(encoding="utf-8")
    assert _PLUGIN_ROOT not in lock, "the lock still claims the retired plugin files"
    # The plugin root and its Claude registration (catalog and settings) retire together.
    assert_changes(
        step.changed,
        (
            "home:.apm/apm.lock.yaml",
            "home:.agents/plugins/*",
            "home:.claude/settings.json",
            *extra,
        ),
        label=path,
    )
    settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    assert not settings.get("enabledPlugins"), settings
    for item in kept:
        assert (home / item).is_file(), f"narrowing removed the copilot output {item}"
    scenario.finish()


def test_mcp_only_package_removal_keeps_unmanaged_and_sibling(lifecycle_world) -> None:
    factory = LocalPackageFactory(lifecycle_world.root / "owned")
    server = {
        "name": "o-mcp-mcp",
        "registry": False,
        "transport": "stdio",
        "command": "apm-lifecycle-inert-server",
        "args": ["--stdio"],
    }
    only = factory.create("o-mcp", version="1.0.0", mcp_dependencies=[server])
    specs = {"o-mcp": str(only.root), "p-exec": lifecycle_world.packages["p-exec"].spec}
    scenario, tree = _start(lifecycle_world, specs, kinds={"o-mcp": ("mcp",)})
    scenario.run("install o-mcp p-exec")
    scenario.run("approve o-mcp")
    scenario.run("approve p-exec")
    scenario.run("install")
    assert {"o-mcp-mcp", "p-exec-mcp"} <= _mcp_servers(lifecycle_world.project)
    (row,) = [r for r in scenario.lock().get_package_dependencies() if r.name == "o-mcp"]
    assert not row.deployed_files, "the MCP-only package owns no files"

    step = scenario.run("uninstall o-mcp")
    assert_changes(
        step.changed,
        (
            "project:apm.yml",
            "project:apm.lock.yaml",
            "project:apm_modules/_local/o-mcp*",
            "project:.mcp.json",
        ),
        label="uninstall MCP-only package",
    )
    assert "o-mcp-mcp" not in _mcp_servers(lifecycle_world.project)
    _survives(scenario, tree, "MCP-only uninstall", "p-exec-mcp")


def test_a_users_lsp_entry_is_never_taken_over(lifecycle_world) -> None:
    """T-6b ``pair|PRIM=lsp|DEPLOYED=unowned`` (#4628, #4697): install keeps the user's entry."""
    scenario, tree = _start(lifecycle_world, ("p-lsp",))
    servers = {"lspServers": {LSP_SERVER: {"command": "mine"}, "user-lsp": {"command": "u"}}}
    scenario.user_write(".github/lsp.json", json.dumps(servers, indent=2) + "\n")
    scenario.run("install p-lsp")
    scenario.run("approve p-lsp")

    # A conflict that wrote no server commits nothing: I7 holds the lockfile
    # byte-identical (#4697).
    locked = scenario.lock_path.read_bytes()
    for line in ("install", "install --force"):
        step = scenario.run(line, exit=1)
        assert f"LSP server '{LSP_SERVER}' conflicts with the existing" in step.output, line
        assert "Preserved that entry" in step.output, line
        tree.assert_intact(line)
        assert scenario.lock_path.read_bytes() == locked, line
        assert_changes(step.changed, (), label=line)
    lock = scenario.lock_path.read_text(encoding="utf-8")
    assert LSP_SERVER not in lock, "the lock claims the user's entry"
    scenario.finish()
