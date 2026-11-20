"""Ownership: unmanaged and sibling assets survive every removal path (#4101).

PROMISE: P-LAW-ownership.preserve_unowned -- APM removes only what it
deployed; user files and other packages' files beside a removed package
survive byte for byte (source: tests/fixtures/lifecycle_bug_ledger.json).
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
from contextlib import nullcontext

import pytest

from tests.lifecycle.scenarios._kit import (
    Known,
    Scenario,
    Tree,
    assert_changes,
    known_defect,
    serve_repository,
)
from tests.lifecycle.world import use_policy
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
NOTE_BLOCKS_UNINSTALL = {
    "R": Known("#4162", r"exited 1, expected \[0\]"),
    "I7": Known(
        "#4162", r"failed uninstall changed (the lockfile|deployed surfaces|durable files)"
    ),
}
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

    # With a user note in the skill directory, uninstall exits 1 after it has
    # already removed the package copy, the SKILL.md and lock rows.
    step = scenario.run("uninstall p-skill", known=NOTE_BLOCKS_UNINSTALL if note else None)
    with known_defect("#4162", r"missing: \['project:apm.yml'\]") if note else nullcontext():
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
    # p-skill is still declared, but its SKILL.md is gone.
    with known_defect("#4162", r"p-skill/skill/copilot") if note else nullcontext():
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
