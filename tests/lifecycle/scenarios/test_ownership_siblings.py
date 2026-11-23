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

T-6b (#4564, #4565) crosses DEPLOYED in {edited, unowned,
present-provenance-unknown} with every other factor. The user's bytes are
declared (``Scenario.user_write``) and must survive (I1-X2). A gate that
keeps them is exempt from I4 only when announced and detected (I4-X2). A
refusal earns credit only when its pair is in ``EXPECTED_REFUSALS``. TGT
reads the declared target set before the step (one target is single, two or
more are multi): narrowed = the step drops at least one previously declared
target; it may add others (a switch counts as narrowed); widened = it adds
at least one and drops none. A native plugin under ~/.agents/plugins is an
APM-managed, fingerprinted unit, so an edit inside it is restored and
announced. Hooks, canvas and LSP entries deploy into the user's project
config, where an edit is user content (#4626).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import PurePosixPath

import pytest
import yaml

from tests.lifecycle.scenarios._kit import (
    PROJECT_HOME_ALLOWED,
    Scenario,
    Tree,
    assert_changes,
    credits,
    serve_repository,
)
from tests.lifecycle.world import use_policy, use_registry
from tests.lifecycle.world.packages import INERT_COMMAND, LSP_SERVER
from tests.lifecycle.world.services import REGISTRY_SERVER
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


def _deployed_plugin_bin(world) -> Scenario:
    """User scope, targets [copilot, claude]: p-native-claude-bin deployed with its bin/."""
    use_policy(world, "allow")
    specs = {
        "p-native-claude-bin": world.packages["p-native-claude-bin:git"].spec,
        "p-skill": world.packages["p-skill"].spec,
    }
    scenario = Scenario(world, specs, scope="user", targets=("copilot", "claude"), policy="allow")
    scenario.run("install p-native-claude-bin")
    scenario.run("approve p-native-claude-bin")
    scenario.run("install --trust-bin")
    assert (world.home / _PLUGIN_BIN).is_file()
    return scenario


def _assert_plugin_unowned(scenario: Scenario) -> None:
    lock = scenario.lock_path.read_text(encoding="utf-8")
    assert _PLUGIN_ROOT not in lock, "the lock still claims the retired plugin files"
    settings_path = scenario.world.home / ".claude/settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert not settings.get("enabledPlugins"), settings


@pytest.mark.parametrize("path", sorted(_NARROW_PATHS))
def test_target_narrow_retires_a_user_scope_plugin_bin(lifecycle_world, path) -> None:
    """#4744: dropping claude retires the plugin bin/ only claude received; the rest stays."""
    scenario = _deployed_plugin_bin(lifecycle_world)
    home = lifecycle_world.home
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
    _assert_plugin_unowned(scenario)
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
    for item in kept:
        assert (home / item).is_file(), f"narrowing removed the copilot output {item}"
    scenario.finish()


def test_target_narrow_keeps_an_edited_plugin_bin_and_says_so(lifecycle_world) -> None:
    """#4744 control: a bin/ the user edited is kept and reported; ownership still retires."""
    scenario = _deployed_plugin_bin(lifecycle_world)
    edited = (lifecycle_world.home / _PLUGIN_BIN).read_text(encoding="ascii") + "# mine\n"
    scenario.user_write(_PLUGIN_BIN, edited)
    scenario.set_targets("copilot")
    step = scenario.run("install --trust-bin")
    assert (lifecycle_world.home / _PLUGIN_BIN).read_text(encoding="ascii") == edited
    assert "Kept ~/.agents/plugins/p-native-claude-bin: its files changed" in step.output, (
        step.output
    )
    _assert_plugin_unowned(scenario)
    scenario.finish()


def test_frozen_refuses_a_dropped_claude_native_plugin(lifecycle_world) -> None:
    """#4744: --frozen refuses the target mismatch (as for files, #4517) and changes nothing."""
    scenario = _deployed_plugin_bin(lifecycle_world)
    scenario.set_targets("copilot")
    step = scenario.run("install --frozen", exit=1)
    assert "no longer declares target(s) claude" in step.output, step.output
    assert _PLUGIN_ROOT in step.output, step.output
    assert_changes(step.changed, (), label="frozen refusal")
    assert (lifecycle_world.home / _PLUGIN_BIN).is_file()
    scenario.run("install --trust-bin")
    assert not (lifecycle_world.home / _PLUGIN_BIN).exists()
    _assert_plugin_unowned(scenario)
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


# -- DEPLOYED x operation pairs (#4101) ------------------------------------------
#
# Each row puts p-skill's deployed skill in one DEPLOYED state the user made
# (Scenario.user_write, I1-X2) and drives one operation. A credited step
# proves the pair: the user's bytes survive (keep) and the step changes
# exactly the pinned set; a declared refusal (EXPECTED_REFUSALS) changes
# nothing and names its cause.

_SKILL = ".agents/skills/p-skill/SKILL.md"
_KEEP = {"project": (_SKILL,)}
# The p-skill unit every subject step acts on: a local-path skill, one target.
# A plain file with no executable beside it reads GATE=none (U2).
_UNIT = {
    "SRC": "local-path",
    "PRIM": "skill",
    "SCOPE": "project",
    "TGT": "single-unchanged",
    "GATE": "none",
}


def _pair(deployed: str, **factors: str) -> dict[str, str]:
    return {"DEPLOYED": deployed, **factors}


_EDITED_OPS = {
    "install": _pair("edited", OP="install", LOCK="consistent", **_UNIT),
    "dry-run": _pair("edited", DRYRUN="on"),
    "update-flag": _pair("edited", UPDATE="on"),
    "select": _pair("edited", SELECT="on"),
    "audit": _pair("edited", OP="audit"),
    "compile": _pair("edited", OP="compile"),
    "lock": _pair("edited", OP="lock"),
    "update": _pair("edited", OP="update"),
    "frozen": _pair("edited", FROZEN="on"),
    "uninstall": _pair("edited", OP="uninstall"),
    "prune": _pair("edited", OP="prune"),
}


def _edited(world) -> Scenario:
    use_policy(world, "allow")
    scenario = Scenario(world, ("p-skill", "p-child"), policy="allow")
    scenario.run("install p-skill")
    edited = (world.project / _SKILL).read_text(encoding="ascii") + "\nMy own note.\n"
    scenario.user_write(_SKILL, edited)
    scenario.source_moved()
    return scenario


def _still(scenario: Scenario, line: str, credit: dict[str, str], **kwargs) -> None:
    """A step that keeps the user's bytes and changes nothing durable."""
    step = scenario.run(line, credit=[credit], keep=_KEEP, **kwargs)
    assert_changes(step.changed, (), label=line)


def test_an_edited_deployed_skill_survives_every_operation(lifecycle_world) -> None:
    scenario = _edited(lifecycle_world)
    ops = _EDITED_OPS
    _still(scenario, "install", ops["install"])
    _still(scenario, "install --dry-run", ops["dry-run"])
    _still(scenario, "install --update", ops["update-flag"])
    _still(scenario, "install p-skill", ops["select"])
    _still(scenario, "audit", ops["audit"], read_only=True)
    _still(scenario, "compile", ops["compile"], read_only=True)
    _still(scenario, "lock", ops["lock"])
    _still(scenario, "update --yes", ops["update"])
    scenario.source_moved()
    scenario.run("install --frozen", exit=1, credit=[ops["frozen"]], keep=_KEEP, refusal=True)
    scenario.run("uninstall p-skill", exit=1, credit=[ops["uninstall"]], keep=_KEEP, refusal=True)

    # Orphaned by hand, the package is pruned; the edited skill stays the user's.
    scenario.drop_from_manifest("p-skill")
    step = scenario.run("prune", credit=[ops["prune"]], keep=_KEEP)
    assert_changes(
        step.changed,
        ("project:apm.lock.yaml", "project:apm_modules/_local/p-skill*"),
        label="prune an edited orphan",
    )
    scenario.finish()


def test_force_replaces_an_edited_deployed_skill(lifecycle_world) -> None:
    scenario = _edited(lifecycle_world)
    scenario.release_user_write(_SKILL)  # the user asks APM to overwrite it
    step = scenario.run("install --force", credit=[_pair("edited", FORCE="on")])
    assert_changes(step.changed, (f"project:{_SKILL}",), label="install --force")
    assert "My own note." not in (lifecycle_world.project / _SKILL).read_text(encoding="ascii")
    scenario.finish()


_USER_SKILL_AT_PATH = "---\nname: p-skill\ndescription: The user's own p-skill.\n---\n\nMine.\n"
_UNOWNED_OPS = {
    "dry-run": _pair("unowned", DRYRUN="on", LOCK="consistent", **_UNIT),
    "install": _pair("unowned", OP="install"),
    "frozen": _pair("unowned", FROZEN="on"),
    "update-flag": _pair("unowned", UPDATE="on"),
    "select": _pair("unowned", SELECT="on"),
    "audit": _pair("unowned", OP="audit"),
    "compile": _pair("unowned", OP="compile"),
    "lock": _pair("unowned", OP="lock"),
    "update": _pair("unowned", OP="update"),
    "uninstall": _pair("unowned", OP="uninstall"),
    "prune": _pair("unowned", OP="prune"),
}


def test_a_user_file_at_a_package_path_survives_every_operation(lifecycle_world) -> None:
    """The user's own file sits where p-skill would deploy; APM never adopts it."""
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, ("p-skill", "p-child"), policy="allow")
    scenario.user_write(_SKILL, _USER_SKILL_AT_PATH)
    scenario.run("install p-child")
    ops = _UNOWNED_OPS
    _still(scenario, "install --dry-run p-skill", ops["dry-run"])
    scenario.run("install p-skill", exit=1, credit=[ops["install"]], keep=_KEEP, refusal=True)
    _still(scenario, "install --frozen", ops["frozen"])
    _still(scenario, "install --update", ops["update-flag"])
    _still(scenario, "install p-child", ops["select"])
    _still(scenario, "audit", ops["audit"], read_only=True)
    _still(scenario, "compile", ops["compile"], read_only=True)
    _still(scenario, "lock", ops["lock"])
    _still(scenario, "update --yes", ops["update"])
    step = scenario.run("uninstall p-child", credit=[ops["uninstall"]], keep=_KEEP)
    assert_changes(
        step.changed,
        (
            "project:apm.yml",
            "project:apm.lock.yaml",
            "project:apm_modules/_local/p-child*",
            "project:.agents/skills/p-child*",
        ),
        label="uninstall beside a user file",
    )
    _still(scenario, "prune", ops["prune"])
    scenario.finish()


_UNKNOWN_OPS = {
    "dry-run": _pair("present-provenance-unknown", DRYRUN="on", **_UNIT),
    "audit": _pair("present-provenance-unknown", OP="audit"),
    "compile": _pair("present-provenance-unknown", OP="compile"),
    "frozen": _pair("present-provenance-unknown", FROZEN="on"),
    "install": _pair("present-provenance-unknown", OP="install"),
    "lock": _pair("present-provenance-unknown", OP="lock"),
    "update-flag": _pair("present-provenance-unknown", UPDATE="on"),
    "select": _pair("present-provenance-unknown", SELECT="on"),
    "force": _pair("present-provenance-unknown", FORCE="on"),
}


def _forget_lock(scenario: Scenario) -> None:
    """The lockfile is lost (a fresh checkout without it): deployed files, provenance unknown."""
    scenario.lock_path.unlink()
    scenario.model = scenario.model.with_lock("absent")


def test_deployed_files_without_a_lock_survive_every_operation(lifecycle_world) -> None:
    """Without a lockfile APM cannot prove it wrote p-skill's files; it keeps their bytes."""
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, ("p-skill",), policy="allow")
    scenario.run("install p-skill")
    scenario.user_write(_SKILL, (lifecycle_world.project / _SKILL).read_text(encoding="ascii"))
    _forget_lock(scenario)
    ops = _UNKNOWN_OPS
    _still(scenario, "install --dry-run", ops["dry-run"])
    _still(scenario, "audit", ops["audit"], read_only=True)
    _still(scenario, "compile", ops["compile"], read_only=True)
    scenario.run("install --frozen", exit=1, credit=[ops["frozen"]], keep=_KEEP, refusal=True)
    for line, key in (
        ("lock", "lock"),
        ("install", "install"),
        ("install --update", "update-flag"),
        ("install p-skill", "select"),
        ("install --force", "force"),
    ):
        step = scenario.run(line, credit=[ops[key]], keep=_KEEP)
        assert_changes(step.changed, ("project:apm.lock.yaml",), label=line)
        _forget_lock(scenario)
    scenario.finish()


_DEPLOYED_STATES = ("edited", "unowned", "present-provenance-unknown")
# Operations that act beside p-skill, never on it.
_BESIDE_OPS = ("mcp-install", "bundle-install", "approve-deny", "marketplace", "pack")


def _beside_credits(deployed: str) -> list[dict[str, str]]:
    git = {"SRC": "git", "PRIM": "skill"}
    rows = [_pair(deployed, OP=op) for op in _BESIDE_OPS]
    return [_pair(deployed, **git), *rows] if deployed != "unowned" else rows


def _git_skill_in(world, deployed: str) -> Scenario:
    """p-skill from git, its deployed skill in *deployed* state (the user's bytes)."""
    use_policy(world, "allow")
    specs = {
        "p-skill": world.packages["p-skill:git"].spec,
        "p-exec": world.packages["p-exec"].spec,
        "p-exec-bundle": world.packages["p-exec:bundle"].spec,
    }
    kinds = {"p-exec-bundle": ("hook", "mcp", "bin")}
    scenario = Scenario(world, specs, policy="allow", kinds=kinds)
    if deployed == "unowned":
        scenario.user_write(_SKILL, _USER_SKILL_AT_PATH)
        return scenario
    scenario.run("install p-skill")
    text = (world.project / _SKILL).read_text(encoding="ascii")
    scenario.user_write(_SKILL, text + "\nMy own note.\n" if deployed == "edited" else text)
    if deployed == "present-provenance-unknown":
        _forget_lock(scenario)
    scenario.source_moved()
    return scenario


@pytest.mark.parametrize("deployed", _DEPLOYED_STATES)
def test_operations_beside_a_users_skill_keep_its_bytes(lifecycle_world, deployed) -> None:
    scenario = _git_skill_in(lifecycle_world, deployed)
    credit = {row.get("OP", "subject"): row for row in _beside_credits(deployed)}
    unknown = deployed == "present-provenance-unknown"

    def act(line: str, key: str, changes: tuple[str, ...], **kwargs) -> None:
        scenario.source_moved()  # the user asks for something new
        step = scenario.run(line, credit=[credit[key]], keep=_KEEP, **kwargs)
        assert_changes(step.changed, changes, label=f"{line}\n{step.output}")
        if unknown and scenario.lock_path.exists():
            _forget_lock(scenario)  # stay without a lockfile for the next row

    if "subject" in credit:
        act("install", "subject", ("project:apm.lock.yaml",) if unknown else ())
    if deployed == "edited":
        # pack verifies every installed file against the lock before it writes.
        scenario.run("pack", exit=1, credit=[credit["pack"]], keep=_KEEP, refusal=True)
    else:
        act("pack", "pack", ("project:.github/plugin*", "project:build*"))
    inline = f"install --mcp p-inline -- {INERT_COMMAND} --stdio"
    # The first MCP write records its evidence against this checkout's
    # installation identity, created here when nothing was deployed before (#4737).
    identity = ("project:.apm-local*",) if deployed == "unowned" else ()
    act(
        inline,
        "mcp-install",
        ("project:.mcp.json", "project:apm.yml", "project:apm.lock.yaml", *identity),
    )
    act("install p-exec-bundle", "bundle-install", ("project:apm_modules*",))
    scenario.run("install p-exec")
    if unknown:
        _forget_lock(scenario)
    if unknown:
        # Without a lockfile APM cannot show p-exec installed, so it approves nothing.
        scenario.run("approve p-exec", exit=1, credit=[credit["approve-deny"]], refusal=True)
    else:
        act("approve p-exec", "approve-deny", ("project:apm.yml",))
    act(
        f"marketplace add {lifecycle_world.services.marketplace_url}",
        "marketplace",
        ("home:.apm/cache*", "home:.apm/marketplaces.json", "home:.apm/marketplaces.lock"),
        forbidden={"project": ("*",)},
        allowed={},
    )
    scenario.finish()


# -- executable primitives --------------------------------------------------------

_HOOK = ".github/hooks/p-exec-session.json"
_CANVAS = ".github/extensions/p-canvas/extension.mjs"
_LSP = ".github/lsp.json"
_EXEC = ("p-exec", "p-canvas", "p-lsp")


def _deployed_executables(world, packages=_EXEC) -> Scenario:
    """*packages* installed and approved: every executable deployed (GATE=allowed)."""
    use_policy(world, "allow")
    scenario = Scenario(world, packages, policy="allow")
    scenario.run("install " + " ".join(packages))
    for package in packages:
        scenario.run(f"approve {package}")
    scenario.run("install")
    return scenario


def _with(path: str, old: str, new: str, world) -> str:
    text = (world.project / path).read_text(encoding="ascii")
    assert old in text, (path, text)
    return text.replace(old, new)


_EDITED_EXEC = [
    _pair("edited", PRIM=prim, GATE="allowed", SRC="local-path")
    for prim in ("hook", "canvas", "lsp")
]


def test_edited_executable_files_are_kept(lifecycle_world) -> None:
    """#4626: install keeps an edited hook, canvas or LSP entry and names each one."""
    scenario = _deployed_executables(lifecycle_world)
    world = lifecycle_world
    scenario.user_write(_HOOK, _with(_HOOK, "echo p-exec", "echo my-own-hook", world))
    scenario.user_write(_CANVAS, _with(_CANVAS, "'p-canvas'", "'my-canvas'", world))
    scenario.user_write(_LSP, _with(_LSP, '"--stdio"', '"--my-flag"', world))
    scenario.source_moved()
    step = scenario.run("install", credit=_EDITED_EXEC)
    assert step.result.returncode == 0, step.output
    for subject in (_HOOK, _CANVAS, f"LSP server '{LSP_SERVER}' in {_LSP}"):
        assert f"Kept {subject}:" in step.output, (subject, step.output)
    scenario.finish()


def _drop_approval(scenario: Scenario, package: str) -> None:
    """The user drops *package*'s approval from apm.yml; the gate stays on."""
    manifest = scenario.world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["executables"]["allow"] = {
        key: value for key, value in data["executables"]["allow"].items() if package not in key
    }
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    scenario.model = scenario.model.revoke(package, scenario.kinds[package])


_SOLO_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo h"}]}]}}


def _hook_package(world) -> Scenario:
    """h-hook: one hook and nothing else, installed and approved."""
    factory = LocalPackageFactory(world.root / "hook-only")
    package = factory.create("h-hook", version="1.0.0")
    factory.add_hook(package, "session", _SOLO_HOOK)
    use_policy(world, "allow")
    kinds = {"h-hook": ("hook",)}
    scenario = Scenario(world, {"h-hook": str(package.root)}, kinds=kinds, policy="allow")
    scenario.run("install h-hook")
    scenario.run("approve h-hook")
    scenario.run("install")
    return scenario


@pytest.mark.parametrize("gate", ["revoked-after-deploy", "unapproved-exec"])
def test_a_closed_gate_retires_a_hook_without_a_lock(lifecycle_world, gate) -> None:
    scenario = _hook_package(lifecycle_world)
    hook = lifecycle_world.project / ".github/hooks/h-hook-session.json"
    assert hook.is_file()
    if gate == "revoked-after-deploy":
        scenario.run("deny h-hook")
    else:
        _drop_approval(scenario, "h-hook")
    _forget_lock(scenario)
    step = scenario.run(
        "install", credit=[_pair("present-provenance-unknown", PRIM="hook", GATE=gate)]
    )
    assert not hook.exists(), step.output
    scenario.finish()


def test_a_withheld_hook_a_checkout_brings_back_is_kept_announced_and_detected(
    lifecycle_world,
) -> None:
    """A gated package's hook file restored by a checkout without a lockfile stays put.

    No lock record proves APM wrote it, so install keeps it but names it, and
    ``audit --ci`` fails naming it until the file is gone.
    """
    scenario = _hook_package(lifecycle_world)
    rel = ".github/hooks/h-hook-session.json"
    hook = lifecycle_world.project / rel
    committed = hook.read_bytes()
    scenario.run("deny h-hook")
    scenario.run("install")
    assert not hook.exists()
    scenario.user_write(rel, committed.decode("ascii"))
    _forget_lock(scenario)
    scenario.run(
        "install",
        keep={"project": (rel,)},
        says=[rf"Kept {re.escape(rel)}: it sits at h-hook's hook path"],
    )
    assert hook.read_bytes() == committed
    audit = scenario.run("audit --ci", read_only=True, exit=1)
    text = " ".join(audit.output.split())
    assert re.search(rf"{re.escape(rel)}: hook path of \S*h-hook \(hooks denied\)", text), text
    assert "delete each named file you did not write yourself" in text, text
    # The printed remedy: delete the file, then the advised audit passes.
    hook.unlink()
    scenario.release_user_write(rel)
    scenario.run("audit --ci", read_only=True, exit=0)
    scenario.finish()


@pytest.mark.parametrize("gate", ["revoked-after-deploy", "unapproved-exec"])
def test_an_edited_hook_outlives_a_closed_gate_announced_and_detected(
    lifecycle_world, gate
) -> None:
    """A closed gate removes the package's executables but keeps the user's edit.

    The kept hook holds the user's bytes only; install says it kept it and
    ``audit --ci`` fails naming it, so the user is never left unaware.
    """
    scenario = _deployed_executables(lifecycle_world, ("p-exec",))
    scenario.user_write(_HOOK, _with(_HOOK, "echo p-exec", "echo my-own-hook", lifecycle_world))
    if gate == "revoked-after-deploy":
        scenario.run("deny p-exec")
    else:
        _drop_approval(scenario, "p-exec")
    scenario.source_moved()
    scenario.run(
        "install",
        credit=[_pair("edited", PRIM="hook", GATE=gate, SRC="local-path")],
        keep={"project": (_HOOK,)},
        says=[rf"Kept user-edited file {re.escape(_HOOK)}"],
    )
    mcp = json.loads((lifecycle_world.project / ".mcp.json").read_text(encoding="utf-8"))
    assert "p-exec-mcp" not in mcp.get("mcpServers", {}), mcp
    audit = scenario.run("audit --ci", read_only=True, exit=1)
    assert re.search(rf"orphaned.*{re.escape(_HOOK)}", " ".join(audit.output.split())), audit.output
    if gate == "revoked-after-deploy":
        # The next install runs with the deny in place: the edit is still kept and said.
        scenario.run(
            "install",
            credit=[_pair("edited", PRIM="hook", GATE="denied-executable", SRC="local-path")],
            keep={"project": (_HOOK,)},
            says=[rf"Kept user-edited file {re.escape(_HOOK)}"],
        )
    scenario.finish()


def test_an_edited_mcp_entry_is_refused_not_overwritten(lifecycle_world) -> None:
    scenario = _deployed_executables(lifecycle_world, ("p-exec",))
    mcp = _with(".mcp.json", '"--stdio"', '"--my-flag"', lifecycle_world)
    scenario.user_write(".mcp.json", mcp)
    scenario.source_moved()
    step = scenario.run(
        "install",
        exit=1,
        credit=[_pair("edited", PRIM="mcp")],
        keep={"project": (".mcp.json",)},
        refusal=True,
    )
    assert "Preserved that connection" in " ".join(step.output.split()), step.output
    scenario.finish()


_UNOWNED_EXEC = {
    "unapproved": [
        _pair("unowned", PRIM="hook", GATE="unapproved-exec", SRC="local-path"),
        _pair("unowned", PRIM="canvas", GATE="unapproved-exec"),
    ],
    "allowed": [_pair("unowned", PRIM="canvas", GATE="allowed")],
    "revoked": [_pair("unowned", PRIM="hook", GATE="revoked-after-deploy")],
    "denied": [_pair("unowned", PRIM="hook", GATE="denied-executable")],
}


def test_users_own_executable_files_at_package_paths_survive(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, ("p-exec", "p-canvas"), policy="allow")
    scenario.user_write(_HOOK, '{"hooks": {}, "version": 1}\n')
    scenario.user_write(_CANVAS, "export default function render() { return 'mine'; }\n")
    keep = {"project": (_HOOK, _CANVAS)}
    rows = _UNOWNED_EXEC
    scenario.run("install p-exec p-canvas", credit=rows["unapproved"], keep=keep)
    scenario.run("approve p-canvas")
    step = scenario.run("install", credit=rows["allowed"], keep=keep)
    assert "local file exists at .github/extensions/p-canvas/extension.mjs" in step.output
    scenario.run("approve p-exec")
    scenario.run("install", keep=keep)  # deploys p-exec's MCP server; the hook stays the user's
    scenario.run("deny p-exec")
    scenario.run("install", credit=rows["revoked"], keep=keep)
    scenario.source_moved()
    scenario.run("install", credit=rows["denied"], keep=keep)
    scenario.finish()


@pytest.mark.parametrize("src", ["inline-mcp", "mcp-registry"])
def test_a_users_mcp_entry_blocks_a_server_of_the_same_name(lifecycle_world, src) -> None:
    """A user's MCP entry of the same name is never taken over.

    An inline server refuses before any write: apm.yml, .github/ and the
    user's .mcp.json stay byte-identical (#4759, I7); a registry server keeps
    the user's entry and says it skipped.
    """
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, (), policy="allow")
    name = "p-inline" if src == "inline-mcp" else REGISTRY_SERVER
    servers = {"mcpServers": {name: {"command": "my-own-server", "args": []}}}
    scenario.user_write(".mcp.json", json.dumps(servers, indent=2) + "\n")
    scenario.source_moved()
    if src == "inline-mcp":
        manifest = (lifecycle_world.project / "apm.yml").read_bytes()
        step = scenario.run(
            _MCP_SOURCES[src],
            exit=1,
            credit=[_pair("unowned", SRC=src)],
            keep={"project": (".mcp.json",)},
            refusal=True,
        )
        assert "MCP server 'p-inline' conflicts with the existing copilot" in step.output
        assert "Preserved that connection" in step.output, step.output
        assert (lifecycle_world.project / "apm.yml").read_bytes() == manifest
        assert not (lifecycle_world.project / ".github").exists(), step.output
        assert_changes(step.changed, (), label=step.line)
    else:
        step = scenario.run(
            _MCP_SOURCES[src],
            credit=[_pair("unowned", PRIM="mcp", SRC=src)],
            keep={"project": (".mcp.json",)},
            says=[rf"{re.escape(name)} already configured, skipping"],
        )
    assert '"my-own-server"' in (lifecycle_world.project / ".mcp.json").read_text(
        encoding="utf-8"
    ), step.output
    scenario.finish()


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
        step = scenario.run(line, exit=1, credit=[_pair("unowned", PRIM="lsp")], refusal=True)
        assert f"LSP server '{LSP_SERVER}' conflicts with the existing" in step.output, line
        assert "Preserved that entry" in step.output, line
        tree.assert_intact(line)
        assert scenario.lock_path.read_bytes() == locked, line
        assert_changes(step.changed, (), label=line)
    lock = scenario.lock_path.read_text(encoding="utf-8")
    assert LSP_SERVER not in lock, "the lock claims the user's entry"
    scenario.finish()


_UNKNOWN_EXEC = [
    _pair("present-provenance-unknown", PRIM=prim, GATE="allowed", SRC="local-path")
    for prim in ("hook", "canvas", "lsp", "mcp")
]


def test_deployed_executables_without_a_lock_are_kept(lifecycle_world) -> None:
    scenario = _deployed_executables(lifecycle_world)
    paths = (_HOOK, _CANVAS, _LSP, ".mcp.json")
    for path in paths:
        scenario.user_write(path, (lifecycle_world.project / path).read_text(encoding="ascii"))
    _forget_lock(scenario)
    step = scenario.run("install", credit=_UNKNOWN_EXEC, keep={"project": paths})
    assert_changes(step.changed, ("project:apm.lock.yaml",), label="install without a lock")
    scenario.finish()


# -- target changes ----------------------------------------------------------------
#
# TGT is <shape>-<change>. The shape is the declared target set BEFORE the step
# (one target is single, two or more are multi). narrowed = the step drops at
# least one previously declared target; it may add others (a switch counts as
# narrowed); widened = it adds at least one and drops none.
_TARGET_CHANGES = {
    "single-widened": (("copilot",), ("copilot", "claude")),
    "single-narrowed": (("copilot",), ("claude",)),
    "multi-unchanged": (("copilot", "claude"), ("copilot", "claude")),
    "multi-widened": (("copilot", "claude"), ("copilot", "claude", "cursor")),
    "multi-narrowed": (("copilot", "claude"), ("copilot",)),
}
_TARGET_ROWS = [(deployed, change) for deployed in _DEPLOYED_STATES for change in _TARGET_CHANGES]


def _targets_in(world, deployed: str, before: tuple[str, ...]) -> Scenario:
    """p-skill's copilot skill in *deployed* state; p-child deployed beside it."""
    use_policy(world, "allow")
    scenario = Scenario(world, ("p-skill", "p-child"), policy="allow", targets=before)
    if deployed == "unowned":
        scenario.user_write(_SKILL, _USER_SKILL_AT_PATH)
        scenario.run("install p-child")
        return scenario
    scenario.run("install p-skill p-child")
    text = (world.project / _SKILL).read_text(encoding="ascii")
    scenario.user_write(_SKILL, text + "\nMy own note.\n" if deployed == "edited" else text)
    if deployed == "present-provenance-unknown":
        _forget_lock(scenario)
    return scenario


_LOCK = "project:apm.lock.yaml"
_CLAUDE = ("project:.claude/skills/p-child*", "project:.claude/skills/p-skill*")
_COPILOT_CHILD = "project:.agents/skills/p-child*"
# What each target change writes. p-skill's claude copy appears only when the
# lock declares p-skill; a dropped copilot copy goes only when the lock proves it.
_TARGET_CHANGED: dict[tuple[str, str], tuple[str, ...]] = {
    ("edited", "single-widened"): (*_CLAUDE, _LOCK),
    ("edited", "single-narrowed"): (*_CLAUDE, _COPILOT_CHILD, _LOCK),
    ("edited", "multi-unchanged"): (),
    ("edited", "multi-widened"): ("project:.cursor",),
    ("edited", "multi-narrowed"): (*_CLAUDE, _LOCK),
    ("unowned", "single-widened"): (_CLAUDE[0], _LOCK),
    ("unowned", "single-narrowed"): (_CLAUDE[0], _COPILOT_CHILD, _LOCK),
    ("unowned", "multi-unchanged"): (),
    ("unowned", "multi-widened"): ("project:.cursor",),
    ("unowned", "multi-narrowed"): (_CLAUDE[0], _LOCK),
    ("present-provenance-unknown", "single-widened"): (*_CLAUDE, _LOCK),
    ("present-provenance-unknown", "single-narrowed"): (*_CLAUDE, _LOCK),
    ("present-provenance-unknown", "multi-unchanged"): (_LOCK,),
    ("present-provenance-unknown", "multi-widened"): ("project:.cursor", _LOCK),
    ("present-provenance-unknown", "multi-narrowed"): (_LOCK,),
}


@pytest.mark.parametrize(("deployed", "change"), _TARGET_ROWS)
def test_a_target_change_keeps_the_users_skill(lifecycle_world, deployed, change) -> None:
    before, after = _TARGET_CHANGES[change]
    scenario = _targets_in(lifecycle_world, deployed, before)
    scenario.set_targets(*after)
    scenario.source_moved()
    step = scenario.run("install", credit=[_pair(deployed, TGT=change)], keep=_KEEP)
    assert_changes(step.changed, _TARGET_CHANGED[deployed, change], label=change)
    scenario.finish()


# -- network -------------------------------------------------------------------------

_CLOSED_PROXY = "http://127.0.0.1:9"
_NET_ROWS = [(deployed, net) for deployed in _DEPLOYED_STATES for net in ("online", "offline")]


@pytest.mark.parametrize(("deployed", "net"), _NET_ROWS)
def test_the_users_skill_survives_online_and_offline(lifecycle_world, deployed, net) -> None:
    """online: p-skill resolves from git; offline: a closed proxy, local sources only."""
    scenario = _git_skill_in(lifecycle_world, deployed)
    services = lifecycle_world.services
    if net == "offline":
        scenario.specs["p-skill"] = lifecycle_world.packages["p-skill"].spec
        for name in ("HTTPS_PROXY", "https_proxy"):
            lifecycle_world.env[name] = _CLOSED_PROXY
    services.reset()
    # Nothing is declared beside a user file, so its online step previews p-skill from git.
    line = (
        "update --yes" if net == "online" and deployed != "unowned" else "install --dry-run p-skill"
    )
    git = {"SRC": "git"} if net == "online" else {}
    step = scenario.run(line, credit=[_pair(deployed, NET=net, **git)], keep=_KEEP)
    assert step.result.returncode == 0, step.output
    hosts = {entry["host"] for entry in services.egress_log()}
    if net == "online":
        assert hosts, "an online update must reach the world's git host"
    else:
        assert not hosts, f"an offline step reached the network: {sorted(hosts)}"
    scenario.finish()


# -- package sources ---------------------------------------------------------------

_SKILL_SOURCES = {
    "marketplace-git": "p-skill:marketplace",
    "pkg-registry": "p-skill:registry",
    "marketplace-local": "p-skill@lifecycle-local",
    "marketplace-file": "p-skill@lifecycle-file",
}
_MARKETPLACES = ("marketplace-git", "marketplace-local", "marketplace-file")


def _local_catalog(world, src: str) -> str:
    """A marketplace on disk: a catalog directory, or one ``marketplace.json`` file."""
    name = "lifecycle-local" if src == "marketplace-local" else "lifecycle-file"
    plugin = {
        "name": "p-skill",
        "description": "Lifecycle world package p-skill",
        "source": {"source": "github", "repo": "lifecycle/p-skill", "ref": "v1.0.0"},
    }
    document = {"name": name, "owner": {"name": "APM Lifecycle"}, "plugins": [plugin]}
    root = world.root / name
    catalog = root / ".claude-plugin" / "marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")
    return str(root if src == "marketplace-local" else catalog)


# A marketplace install may refresh the catalog cache under HOME.
_MARKET_HOME = {"home": (*PROJECT_HOME_ALLOWED, ".apm/cache/marketplace")}


def _source_fields(src: str) -> dict[str, object]:
    return {"allowed": _MARKET_HOME} if src in _MARKETPLACES else {}


_SOURCE_ROWS = [(deployed, src) for deployed in _DEPLOYED_STATES for src in _SKILL_SOURCES]


def _skill_from(world, deployed: str, src: str) -> Scenario:
    """p-skill installed from *src*, its deployed skill in *deployed* state."""
    use_policy(world, "allow")
    if src == "pkg-registry":
        use_registry(world)
    spec = _SKILL_SOURCES[src]
    spec = world.packages[spec].spec if spec in world.packages else spec
    scenario = Scenario(world, {"p-skill": spec}, policy="allow")
    if src in _MARKETPLACES:
        source = (
            world.services.marketplace_url
            if src == "marketplace-git"
            else _local_catalog(world, src)
        )
        scenario.run(f"marketplace add {source}", forbidden={"project": ("*",)}, allowed={})
    if deployed == "unowned":
        scenario.user_write(_SKILL, _USER_SKILL_AT_PATH)
        return scenario
    scenario.run("install p-skill", **_source_fields(src))
    text = (world.project / _SKILL).read_text(encoding="ascii")
    scenario.user_write(_SKILL, text + "\nMy own note.\n" if deployed == "edited" else text)
    if deployed == "present-provenance-unknown":
        _forget_lock(scenario)
    scenario.source_moved()
    return scenario


@pytest.mark.parametrize(("deployed", "src"), _SOURCE_ROWS)
def test_a_users_skill_from_each_source_survives_install(lifecycle_world, deployed, src) -> None:
    scenario = _skill_from(lifecycle_world, deployed, src)
    line = "install --dry-run p-skill" if deployed == "unowned" else "install"
    credit = [_pair(deployed, SRC=src, PRIM="skill")]
    step = scenario.run(line, credit=credit, keep=_KEEP, **_source_fields(src))
    expected: tuple[str, ...] = ()
    if deployed == "present-provenance-unknown":
        expected = ("project:apm.lock.yaml",)
    elif deployed == "unowned" and src == "marketplace-git":
        expected = ("home:.apm/cache/marketplace*",)  # the first resolve fetches the catalog
    assert_changes(step.changed, expected, label=src)
    scenario.finish()


# -- native plugin, plugin bin/ and local bundle -----------------------------------

_NATIVE_SKILL = ".agents/skills/p-native-claude/SKILL.md"
_USER_NATIVE_SKILL = (
    "---\nname: p-native-claude\ndescription: The user's own skill.\n---\n\nMine.\n"
)


@pytest.mark.parametrize("deployed", _DEPLOYED_STATES)
def test_a_native_plugins_skill_in_the_users_hands_survives_install(
    lifecycle_world, deployed
) -> None:
    use_policy(lifecycle_world, "allow")
    # cursor splits every plugin into loose skills; claude and copilot install a
    # Claude plugin whole at project scope (#3921), where the skill is not a loose file.
    targets = ("cursor",)
    scenario = Scenario(lifecycle_world, ("p-native-claude",), policy="allow", targets=targets)
    if deployed == "unowned":
        scenario.user_write(_NATIVE_SKILL, _USER_NATIVE_SKILL)
        line = "install --dry-run p-native-claude"
    else:
        scenario.run("install p-native-claude")
        text = (lifecycle_world.project / _NATIVE_SKILL).read_text(encoding="ascii")
        scenario.user_write(_NATIVE_SKILL, text + ("\nMine.\n" if deployed == "edited" else ""))
        if deployed == "present-provenance-unknown":
            _forget_lock(scenario)
        line = "install"
    step = scenario.run(
        line,
        credit=[_pair(deployed, SRC="native-plugin")],
        keep={"project": (_NATIVE_SKILL,)},
    )
    unknown = deployed == "present-provenance-unknown"
    assert_changes(step.changed, ("project:apm.lock.yaml",) if unknown else (), label=line)
    scenario.finish()


def test_a_native_plugin_is_one_gated_cell_at_project_scope(lifecycle_world) -> None:
    """A Claude plugin at project scope is one gated native cell per target (#4808).

    Its root under .agents/plugins and each target's enabledPlugins declaration
    are observed after every step: an approved install leaves both whole, and a
    deny retires both (I4), so no loose copy and no stale declaration remain.
    """
    use_policy(lifecycle_world, "allow")
    targets = ("claude", "copilot")
    # The carried skill and MCP kinds are observed too, so a loose copy fails the model.
    kinds = {"p-native-claude": ("native", "skill", "mcp")}
    scenario = Scenario(
        lifecycle_world, ("p-native-claude",), kinds=kinds, policy="allow", targets=targets
    )
    scenario.run("install p-native-claude")
    scenario.run("approve p-native-claude")
    scenario.run("install")
    scenario.assert_deployed()
    scenario.run("deny p-native-claude")
    scenario.run("install")
    scenario.assert_deployed()
    scenario.finish()


def test_a_native_plugin_with_bin_deploys_whole_only_when_trusted(lifecycle_world) -> None:
    """At project scope an approved plugin that ships bin/ is parked whole until
    ``--trust-bin`` (claude_plugins/admission.py); then root and declaration land."""
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(
        lifecycle_world, ("p-native-claude-bin",), policy="allow", targets=("claude",)
    )
    scenario.run("install p-native-claude-bin")
    scenario.run("approve p-native-claude-bin")
    step = scenario.run("install")
    assert not scenario.deployed()[0], sorted(step.changed)
    # The model carries no bin-trust posture: trusting bin/ is a new input.
    scenario.source_moved()
    scenario.run("install --trust-bin")
    scenario.assert_deployed()
    scenario.finish()


_PLUGIN_BIN = ".agents/plugins/p-native-claude-bin/bin/p-native-claude-bin"


def _bin_plugin(world) -> Scenario:
    """p-native-claude-bin from git at user scope for Claude: only there bin/ deploys."""
    use_policy(world, "allow")
    specs = {"p-native-claude-bin": world.packages["p-native-claude-bin:git"].spec}
    return Scenario(world, specs, scope="user", targets=("claude",), policy="allow")


def _deploy_bin(scenario: Scenario, **fields) -> object:
    scenario.run("install p-native-claude-bin")
    scenario.run("approve p-native-claude-bin")
    return scenario.run("install --trust-bin", **fields)


def test_a_users_file_at_the_plugin_bin_path_refuses_the_plugin(lifecycle_world) -> None:
    scenario = _bin_plugin(lifecycle_world)
    scenario.user_write(_PLUGIN_BIN, "#!/bin/sh\necho mine\n")
    step = _deploy_bin(
        scenario,
        exit=1,
        credit=[_pair("unowned", PRIM="bin")],
        keep={"home": (_PLUGIN_BIN,)},
        refusal=True,
    )
    assert "APM never replaces content it did not deploy" in step.output, step.output
    scenario.finish()


def test_a_plugin_bin_without_a_lock_is_kept(lifecycle_world) -> None:
    scenario = _bin_plugin(lifecycle_world)
    _deploy_bin(scenario)
    text = (lifecycle_world.home / _PLUGIN_BIN).read_text(encoding="ascii")
    scenario.user_write(_PLUGIN_BIN, text)
    _forget_lock(scenario)
    step = scenario.run(
        "install --trust-bin",
        credit=[_pair("present-provenance-unknown", PRIM="bin")],
        keep={"home": (_PLUGIN_BIN,)},
    )
    assert "APM left them in place" in " ".join(step.output.split()), step.output
    scenario.finish()


def test_an_edited_plugin_bin_is_restored_and_said(lifecycle_world) -> None:
    """A native plugin is one unit: an edit inside it is restored, and install says so.

    ~/.agents/plugins/<name> is an APM-managed, fingerprinted unit
    (claude_plugins/registrar.py), not a place for user files, so the restore
    is designed and announced (I7). Contrast #4626: hooks, canvas and LSP
    entries deploy into the user's project config, where an edit is user
    content that install must keep.
    """
    scenario = _bin_plugin(lifecycle_world)
    _deploy_bin(scenario)
    path = lifecycle_world.home / _PLUGIN_BIN
    package = path.read_bytes()
    path.write_bytes(package + b"echo mine\n")
    scenario.source_moved()
    step = scenario.run(
        "install --trust-bin",
        credit=[_pair("edited", PRIM="bin")],
        says=[r"Restored ~/\.agents/plugins/p-native-claude-bin: its files differed"],
    )
    assert path.read_bytes() == package, step.output
    scenario.finish()


_BUNDLE_HOOK = ".github/hooks/p-exec-hooks.json"
_BUNDLE_KINDS = {"p-exec-bundle": ("hook", "mcp", "bin")}


def _bundle(world) -> Scenario:
    use_policy(world, "allow")
    specs = {"p-exec-bundle": world.packages["p-exec:bundle"].spec}
    return Scenario(world, specs, policy="allow", kinds=_BUNDLE_KINDS)


def _approve_bundle(scenario: Scenario, output: str) -> None:
    """Pin this exact bundle in apm.yml, as the install output tells the user to."""
    found = re.search(r'"(p-exec#[^"]+@sha256:[0-9a-f]+)"', output)
    assert found, output
    manifest = scenario.world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    allow = data.setdefault("executables", {}).setdefault("allow", {})
    allow[found.group(1)] = {"hooks": True, "mcp": True, "bin": True}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    scenario.model = scenario.model.approve("p-exec-bundle", _BUNDLE_KINDS["p-exec-bundle"])
    scenario.source_moved()


_SKIPPED_BUNDLE_HOOK = rf"Skipping {re.escape(_BUNDLE_HOOK)} -- local file exists"


@pytest.mark.parametrize("deployed", _DEPLOYED_STATES)
def test_a_local_bundle_never_replaces_the_users_hook(lifecycle_world, deployed) -> None:
    """A local bundle never replaces the user's hook, in every deployed state (#4634 fixed)."""
    scenario = _bundle(lifecycle_world)
    if deployed == "unowned":
        scenario.user_write(_BUNDLE_HOOK, '{"hooks": {}, "version": 1}\n')
    first = scenario.run("install p-exec-bundle")
    _approve_bundle(scenario, first.output)
    unknown = deployed == "present-provenance-unknown"
    credit = {
        "credit": [_pair(deployed, SRC="local-bundle")],
        "keep": {"project": (_BUNDLE_HOOK,)},
        "says": [_SKIPPED_BUNDLE_HOOK],
    }
    if deployed == "unowned":
        scenario.run("install p-exec-bundle", **credit)
        scenario.pair()
        scenario.finish()
        return
    scenario.run("install p-exec-bundle")
    text = (lifecycle_world.project / _BUNDLE_HOOK).read_text(encoding="ascii")
    if deployed == "edited":
        text = text.replace("echo p-exec", "echo my-own-hook")
    scenario.user_write(_BUNDLE_HOOK, text)
    if unknown:
        _forget_lock(scenario)
    scenario.run("install p-exec-bundle", **credit)
    if deployed == "edited":
        audit = scenario.run("audit --ci", read_only=True, exit=1)
        assert re.search(rf"modified.*{re.escape(_BUNDLE_HOOK)}", " ".join(audit.output.split()))
    else:
        scenario.pair()
    scenario.finish()


_MCP_SOURCES = {
    "mcp-registry": f"install --mcp {REGISTRY_SERVER}",
    "inline-mcp": f"install --mcp p-inline -- {INERT_COMMAND} --stdio",
}


@pytest.mark.parametrize("src", sorted(_MCP_SOURCES))
def test_an_edited_mcp_entry_from_each_source_is_refused(lifecycle_world, src) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, (), policy="allow")
    scenario.run(_MCP_SOURCES[src])
    mcp = json.loads((lifecycle_world.project / ".mcp.json").read_text(encoding="utf-8"))
    for server in mcp["mcpServers"].values():
        server["env"] = {"MY_OWN": "1"}
    scenario.user_write(".mcp.json", json.dumps(mcp, indent=2) + "\n")
    scenario.source_moved()
    step = scenario.run(
        "install",
        exit=1,
        credit=[_pair("edited", SRC=src)],
        keep={"project": (".mcp.json",)},
        refusal=True,
    )
    assert "Preserved that connection" in " ".join(step.output.split()), step.output
    scenario.finish()


@pytest.mark.parametrize("src", sorted(_MCP_SOURCES))
def test_an_mcp_entry_without_a_lock_is_kept(lifecycle_world, src) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, (), policy="allow")
    scenario.run(_MCP_SOURCES[src])
    text = (lifecycle_world.project / ".mcp.json").read_text(encoding="utf-8")
    scenario.user_write(".mcp.json", text)
    _forget_lock(scenario)
    step = scenario.run(
        "install",
        credit=[_pair("present-provenance-unknown", SRC=src)],
        keep={"project": (".mcp.json",)},
    )
    assert_changes(step.changed, ("project:apm.lock.yaml",), label=src)
    scenario.finish()


_UNKNOWN_REMOVALS = {
    "update": _pair("present-provenance-unknown", OP="update"),
    "uninstall": _pair("present-provenance-unknown", OP="uninstall"),
    "prune": _pair("present-provenance-unknown", OP="prune"),
}


def test_removals_without_a_lock_never_delete_the_skill(lifecycle_world) -> None:
    """Without a lockfile APM cannot prove it wrote p-skill's skill; it never deletes it."""
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, ("p-skill",), policy="allow")
    scenario.run("install p-skill")
    text = (lifecycle_world.project / _SKILL).read_text(encoding="ascii")
    scenario.user_write(_SKILL, text)
    rows = _UNKNOWN_REMOVALS
    _forget_lock(scenario)
    scenario.source_moved()
    step = scenario.run("update --yes", credit=[rows["update"]], keep=_KEEP)
    assert_changes(step.changed, (_LOCK,), label="update without a lock")
    _forget_lock(scenario)
    scenario.source_moved()
    step = scenario.run("uninstall p-skill", credit=[rows["uninstall"]], keep=_KEEP)
    assert_changes(
        step.changed,
        ("project:apm.yml", "project:apm_modules/_local/p-skill*"),
        label="uninstall without a lock",
    )
    scenario.run("install p-skill")
    _forget_lock(scenario)
    scenario.drop_from_manifest("p-skill")
    step = scenario.run("prune", credit=[rows["prune"]], keep=_KEEP)
    assert_changes(step.changed, ("project:apm_modules/_local/p-skill*",), label="prune")
    scenario.finish()


def test_force_replaces_a_users_file_at_a_package_path(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, ("p-skill",), policy="allow")
    scenario.user_write(_SKILL, _USER_SKILL_AT_PATH)
    scenario.release_user_write(_SKILL)  # the user asks APM to overwrite it
    step = scenario.run("install --force p-skill", credit=[_pair("unowned", FORCE="on")])
    assert f"project:{_SKILL}" in step.changed
    assert "Mine." not in (lifecycle_world.project / _SKILL).read_text(encoding="ascii")
    scenario.finish()


# -- #4626: edited executable outputs are kept like any managed file -------------
#
# PROMISE: P-LAW-ownership.preserve_unowned via I1-X2 -- apm install never
# rewrites a deployed hook file, copied hook script, canvas file or APM-managed
# LSP entry unless deployment evidence proves APM wrote its current bytes
# (source: integration/base_integrator.py BaseIntegrator.keeps_unproven_file).

EDITED = "o-exec"
_HOOK_JSON = f".github/hooks/{EDITED}-session.json"
_SCRIPT = f".github/hooks/scripts/{EDITED}/session/run.sh"
_CANVAS_ENTRY = f".github/extensions/{EDITED}/extension.mjs"
_CANVAS_ICON = f".github/extensions/{EDITED}/icon.txt"
_LSP = ".github/lsp.json"
EDITED_LSP = "o-lsp"
_LSP_SERVER = f"{EDITED_LSP}-server"
_EXEC_KINDS = ("hook", "canvas", "lsp")


def _author_exec(
    world, revision: str = "v1", *, lsp: str | None = None, icon: str | None = ""
) -> dict[str, str]:
    """Author ``o-exec`` (hook + copied script, two-member canvas) at *revision*.

    ``o-lsp`` is authored at *lsp* and the canvas icon at *icon* (default:
    *revision*; ``None`` ships no icon).
    """
    owned = world.root / "owned"
    for name in (EDITED, EDITED_LSP):
        if (owned / name).exists():
            shutil.rmtree(owned / name)
    factory = LocalPackageFactory(owned)
    package = factory.create(EDITED, version="1.0.0")
    command = f"sh ./session/run.sh {revision}"
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    factory.add_hook(package, "session", hook)
    script = f"#!/bin/sh\necho {revision}\n"
    factory.add_hook_asset(package, "session", PurePosixPath("run.sh"), script, executable=True)
    extension = f"export default function render() {{\n  return '{revision}';\n}}\n"
    assets = {} if icon is None else {PurePosixPath("icon.txt"): f"{icon or revision}\n".encode()}
    factory.add_canvas(package, EDITED, extension, assets=assets)
    server = {
        "name": _LSP_SERVER,
        "command": "apm-lifecycle-inert-server",
        "args": ["--stdio", f"--{lsp or revision}"],
        "extensionToLanguage": {".fixture": "fixture"},
    }
    lsp = factory.create(EDITED_LSP, version="1.0.0", lsp_dependencies=[server])
    return {EDITED: str(package.root), EDITED_LSP: str(lsp.root)}


def _exec_scenario(world) -> tuple[Scenario, Tree]:
    kinds = {EDITED: ("hook", "canvas"), EDITED_LSP: ("lsp",)}
    scenario, tree = _start(world, _author_exec(world), kinds=kinds)
    scenario.run(f"install {EDITED} {EDITED_LSP}")
    scenario.run(f"approve {EDITED} {EDITED_LSP}")
    scenario.run("install")
    scenario.assert_deployed()
    for relative in (_HOOK_JSON, _SCRIPT, _CANVAS_ENTRY, _CANVAS_ICON, _LSP):
        assert (world.project / relative).is_file(), relative
    return scenario, tree


def _claims(scenario: Scenario) -> dict[str, object]:
    """The lock's claims on o-exec's files and LSP server, as the kept outputs see them."""
    lock = scenario.lock()
    (row,) = [dep for dep in lock.get_package_dependencies() if dep.repo_url.endswith(EDITED)]
    hashes = dict(row.deployed_file_hashes)
    return {
        **{path: hashes.get(path) for path in (_HOOK_JSON, _SCRIPT, _CANVAS_ENTRY, _CANVAS_ICON)},
        "lsp": lock.lsp_configs.get(_LSP_SERVER),
    }


def _edit_lsp_entry(scenario: Scenario) -> str:
    """Edit o-exec's LSP entry; add an unrelated top-level key and a user server."""
    path = scenario.world.project / _LSP
    config = json.loads(path.read_text(encoding="utf-8"))
    config["lspServers"][_LSP_SERVER]["args"] = ["--stdio", "--mine"]
    config["lspServers"]["user-server"] = {"command": "user-lsp", "args": []}
    config["userSetting"] = True
    return scenario.user_write(_LSP, json.dumps(config, indent=2) + "\n")


def _kept(step, *subjects: str) -> None:
    for subject in subjects:
        assert re.search(rf"\[!\]\s*Kept {re.escape(subject)}:", step.result.stdout) or re.search(
            rf"Kept {re.escape(subject)}:", step.output
        ), f"no Kept line for {subject}:\n{step.result.stdout}\n{step.result.stderr}"


def _edit(world, relative: str, text: str) -> str:
    return (world.project / relative).read_text(encoding="utf-8") + text


def test_edited_outputs_keep_unrelated_lsp_keys_and_yield_to_force(lifecycle_world) -> None:
    """A proven edit with nothing new to deploy is kept and named; install exits 0."""
    scenario, tree = _exec_scenario(lifecycle_world)
    world = lifecycle_world
    scenario.user_write(
        _HOOK_JSON, _edit(world, _HOOK_JSON, "").replace("run.sh v1", "run.sh mine")
    )
    scenario.user_write(_SCRIPT, _edit(world, _SCRIPT, "echo mine\n"))
    scenario.user_write(_CANVAS_ENTRY, _edit(world, _CANVAS_ENTRY, "// mine\n"))
    _edit_lsp_entry(scenario)
    claims = _claims(scenario)

    step = scenario.run("install")
    assert step.result.returncode == 0
    _kept(step, _HOOK_JSON, _SCRIPT, _CANVAS_ENTRY, f"LSP server '{_LSP_SERVER}' in {_LSP}")
    assert_changes(step.changed, (), label="install keeps every edit")
    assert _claims(scenario) == claims, "a kept edit became APM's baseline"
    tree.assert_intact("install")
    scenario.finish()

    # --force still overwrites.
    for relative in (_HOOK_JSON, _SCRIPT, _CANVAS_ENTRY, _LSP):
        scenario.release_user_write(relative)
    forced = scenario.run("install --force")
    assert "Kept" not in forced.output
    assert "mine" not in (world.project / _CANVAS_ENTRY).read_text(encoding="utf-8")
    lsp = json.loads((world.project / _LSP).read_text(encoding="utf-8"))
    assert lsp["lspServers"][_LSP_SERVER]["args"] == ["--stdio", "--v1"]
    assert lsp["userSetting"] is True and "user-server" in lsp["lspServers"]
    scenario.finish()


def test_edited_outputs_block_an_update_and_unedited_ones_update(lifecycle_world) -> None:
    """A proven edit that blocks an update is kept and named; install exits 1.

    Unedited outputs update silently, except a canvas bundle with a kept
    member, which is left whole.
    """
    scenario, tree = _exec_scenario(lifecycle_world)
    world = lifecycle_world
    scenario.user_write(_SCRIPT, _edit(world, _SCRIPT, "echo mine\n"))
    scenario.user_write(_CANVAS_ENTRY, _edit(world, _CANVAS_ENTRY, "// mine\n"))
    claims = _claims(scenario)
    icon = (world.project / _CANVAS_ICON).read_bytes()
    _author_exec(world, "v2")
    scenario.source_moved()
    # Pinned approvals cover exact definitions: the user approves v2 first.
    scenario.run(f"approve {EDITED} {EDITED_LSP}")

    step = scenario.run("install", exit=1)
    _kept(step, _SCRIPT, _CANVAS_ENTRY)
    assert f"Kept {_HOOK_JSON}" not in step.output
    assert "run.sh v2" in (world.project / _HOOK_JSON).read_text(encoding="utf-8")
    assert (world.project / _CANVAS_ICON).read_bytes() == icon, "a kept canvas bundle changed"
    after = _claims(scenario)
    for relative in (_SCRIPT, _CANVAS_ENTRY, _CANVAS_ICON):
        assert after[relative] == claims[relative], f"{relative}: the kept claim moved"
    tree.assert_intact("blocked update")

    # The printed remedy: move the kept files aside, then run 'apm install'.
    for relative in (_SCRIPT, _CANVAS_ENTRY):
        moved = world.root / "aside" / relative
        moved.parent.mkdir(parents=True, exist_ok=True)
        (world.project / relative).rename(moved)
        scenario.release_user_write(relative)
    restored = scenario.run("install")
    assert "Kept" not in restored.output
    assert (world.project / _SCRIPT).read_text(encoding="utf-8") == "#!/bin/sh\necho v2\n"
    assert (world.project / _CANVAS_ICON).read_text(encoding="utf-8") == "v2\n"
    lsp = json.loads((world.project / _LSP).read_text(encoding="utf-8"))
    assert lsp["lspServers"][_LSP_SERVER]["args"] == ["--stdio", "--v2"]

    # An edited LSP entry blocks the next server update; unrelated keys survive.
    _edit_lsp_entry(scenario)
    locked = scenario.lock().lsp_configs[_LSP_SERVER]
    _author_exec(world, "v2", lsp="v3")
    scenario.run(f"approve {EDITED} {EDITED_LSP}")
    blocked = scenario.run("install", exit=1)
    _kept(blocked, f"LSP server '{_LSP_SERVER}' in {_LSP}")
    assert scenario.lock().lsp_configs[_LSP_SERVER] == locked, "a kept entry became the baseline"
    tree.assert_intact("blocked LSP update")
    scenario.finish()


def test_a_kept_canvas_edit_holds_back_its_other_members(lifecycle_world) -> None:
    """An edit to an unchanged member blocks the bundle's other updates (exit 1).

    The upgrade first changes only icon.txt, then drops it: the bundle stays
    whole, icon.txt keeps its bytes and its prior lock claim both times.
    """
    scenario, tree = _exec_scenario(lifecycle_world)
    world = lifecycle_world
    scenario.user_write(_CANVAS_ENTRY, _edit(world, _CANVAS_ENTRY, "// mine\n"))
    claims = _claims(scenario)
    icon = (world.project / _CANVAS_ICON).read_bytes()

    for label, upgrade in (("icon changed", {"icon": "v2"}), ("icon dropped", {"icon": None})):
        _author_exec(world, **upgrade)
        scenario.source_moved()
        scenario.run(f"approve {EDITED} {EDITED_LSP}")
        step = scenario.run("install", exit=1)
        _kept(step, _CANVAS_ENTRY)
        assert (world.project / _CANVAS_ICON).read_bytes() == icon, f"{label}: icon changed"
        after = _claims(scenario)
        for relative in (_CANVAS_ENTRY, _CANVAS_ICON):
            assert after[relative] == claims[relative], f"{label}: {relative} claim moved"
        tree.assert_intact(label)
    scenario.finish()


def test_an_edited_member_the_upgrade_drops_keeps_the_bundle_whole(lifecycle_world) -> None:
    """The upgrade drops an edited icon.txt and changes extension.mjs: exit 1, bundle whole."""
    scenario, tree = _exec_scenario(lifecycle_world)
    world = lifecycle_world
    scenario.user_write(_CANVAS_ICON, _edit(world, _CANVAS_ICON, "mine\n"))
    claims = _claims(scenario)
    extension = (world.project / _CANVAS_ENTRY).read_bytes()
    _author_exec(world, "v2", icon=None)
    scenario.source_moved()
    scenario.run(f"approve {EDITED} {EDITED_LSP}")

    step = scenario.run("install", exit=1)
    _kept(step, _CANVAS_ICON)
    assert (world.project / _CANVAS_ENTRY).read_bytes() == extension, "half-updated canvas"
    after = _claims(scenario)
    for relative in (_CANVAS_ENTRY, _CANVAS_ICON):
        assert after[relative] == claims[relative], f"{relative}: the kept claim moved"
    tree.assert_intact("dropped edited member")
    scenario.finish()


def _declare_root_lsp(world, revision: str) -> None:
    """Declare two LSP servers in the project's own apm.yml (no APM packages)."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"]["lsp"] = [
        {
            "name": name,
            "command": INERT_COMMAND,
            "args": ["--stdio", f"--{revision}"],
            "extensionToLanguage": {f".{name}": name},
        }
        for name in ("root-a", "root-b")
    ]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_a_kept_manifest_lsp_edit_fails_and_names_what_it_wrote(lifecycle_world) -> None:
    """Manifest-only LSP: one edited server blocks its update (exit 1), the other updates."""
    world = lifecycle_world
    scenario, tree = _start(world, {})
    _declare_root_lsp(world, "v1")
    scenario.source_moved()
    scenario.run("install")
    config = json.loads((world.project / _LSP).read_text(encoding="utf-8"))
    config["lspServers"]["root-a"]["args"] = ["--stdio", "--mine"]
    edited = config["lspServers"]["root-a"]
    scenario.user_write(_LSP, json.dumps(config, indent=2) + "\n")
    scenario.release_user_write(_LSP)
    locked = scenario.lock().lsp_configs["root-a"]
    _declare_root_lsp(world, "v2")
    scenario.source_moved()

    step = scenario.run("install", exit=1)
    _kept(step, f"LSP server 'root-a' in {_LSP}")
    assert f"This install wrote 1 file(s), recorded in apm.lock.yaml: {_LSP}" in step.output
    servers = json.loads((world.project / _LSP).read_text(encoding="utf-8"))["lspServers"]
    assert servers["root-a"] == edited
    assert servers["root-b"]["args"] == ["--stdio", "--v2"]
    assert scenario.lock().lsp_configs["root-a"] == locked, "a kept edit became the baseline"
    tree.assert_intact("manifest LSP keep")
    scenario.finish()


def test_unproven_mismatch_is_kept(lifecycle_world) -> None:
    """Without a receipt or locked render proving APM wrote it, a mismatch is kept; exit 1."""
    scenario, tree = _exec_scenario(lifecycle_world)
    world = lifecycle_world
    for receipts in (
        world.home / ".apm" / "deployment-receipts",
        world.apm_home / "deployment-receipts",
    ):
        if receipts.exists():
            shutil.rmtree(receipts)
    scenario.user_write(
        _HOOK_JSON, _edit(world, _HOOK_JSON, "").replace("run.sh v1", "run.sh mine")
    )
    claims = _claims(scenario)

    step = scenario.run("install", exit=1)
    _kept(step, _HOOK_JSON)
    assert "no local deployment receipt" in step.output
    assert _claims(scenario) == claims, "an unproven file became APM's baseline"
    tree.assert_intact("unproven mismatch")
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        "test_an_edited_deployed_skill_survives_every_operation": list(_EDITED_OPS.values()),
        "test_force_replaces_an_edited_deployed_skill": [_pair("edited", FORCE="on")],
        "test_a_user_file_at_a_package_path_survives_every_operation": list(_UNOWNED_OPS.values()),
        "test_deployed_files_without_a_lock_survive_every_operation": list(_UNKNOWN_OPS.values()),
        "test_edited_executable_files_are_kept": _EDITED_EXEC,
        "test_an_edited_mcp_entry_is_refused_not_overwritten": [_pair("edited", PRIM="mcp")],
        "test_users_own_executable_files_at_package_paths_survive": [
            row for rows in _UNOWNED_EXEC.values() for row in rows
        ],
        "test_a_users_lsp_entry_is_never_taken_over": [_pair("unowned", PRIM="lsp")],
        "test_a_users_mcp_entry_blocks_a_server_of_the_same_name[inline-mcp]": [
            _pair("unowned", SRC="inline-mcp")
        ],
        "test_a_users_mcp_entry_blocks_a_server_of_the_same_name[mcp-registry]": [
            _pair("unowned", PRIM="mcp", SRC="mcp-registry")
        ],
        "test_deployed_executables_without_a_lock_are_kept": _UNKNOWN_EXEC,
        **{
            f"test_a_closed_gate_retires_a_hook_without_a_lock[{gate}]": [
                _pair("present-provenance-unknown", PRIM="hook", GATE=gate)
            ]
            for gate in ("revoked-after-deploy", "unapproved-exec")
        },
        **{
            f"test_a_native_plugins_skill_in_the_users_hands_survives_install[{deployed}]": [
                _pair(deployed, SRC="native-plugin")
            ]
            for deployed in _DEPLOYED_STATES
        },
        "test_a_users_file_at_the_plugin_bin_path_refuses_the_plugin": [
            _pair("unowned", PRIM="bin")
        ],
        "test_a_plugin_bin_without_a_lock_is_kept": [
            _pair("present-provenance-unknown", PRIM="bin")
        ],
        "test_an_edited_plugin_bin_is_restored_and_said": [_pair("edited", PRIM="bin")],
        **{
            f"test_a_local_bundle_never_replaces_the_users_hook[{deployed}]": [
                _pair(deployed, SRC="local-bundle")
            ]
            for deployed in _DEPLOYED_STATES
        },
        **{
            f"test_an_edited_hook_outlives_a_closed_gate_announced_and_detected[{gate}]": [
                _pair("edited", PRIM="hook", GATE=level, SRC="local-path")
                for level in (
                    (gate, "denied-executable") if gate == "revoked-after-deploy" else (gate,)
                )
            ]
            for gate in ("revoked-after-deploy", "unapproved-exec")
        },
        "test_removals_without_a_lock_never_delete_the_skill": list(_UNKNOWN_REMOVALS.values()),
        "test_force_replaces_a_users_file_at_a_package_path": [_pair("unowned", FORCE="on")],
        **{
            f"test_the_users_skill_survives_online_and_offline[{deployed}-{net}]": [
                _pair(deployed, NET=net, **({"SRC": "git"} if net == "online" else {}))
            ]
            for deployed, net in _NET_ROWS
        },
        **{
            f"test_a_users_skill_from_each_source_survives_install[{deployed}-{src}]": [
                _pair(deployed, SRC=src, PRIM="skill")
            ]
            for deployed, src in _SOURCE_ROWS
        },
        **{
            f"test_an_edited_mcp_entry_from_each_source_is_refused[{src}]": [
                _pair("edited", SRC=src)
            ]
            for src in _MCP_SOURCES
        },
        **{
            f"test_an_mcp_entry_without_a_lock_is_kept[{src}]": [
                _pair("present-provenance-unknown", SRC=src)
            ]
            for src in _MCP_SOURCES
        },
        **{
            f"test_a_target_change_keeps_the_users_skill[{deployed}-{change}]": [
                _pair(deployed, TGT=change)
            ]
            for deployed, change in _TARGET_ROWS
        },
        **{
            f"test_operations_beside_a_users_skill_keep_its_bytes[{deployed}]": _beside_credits(
                deployed
            )
            for deployed in _DEPLOYED_STATES
        },
    },
)
