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
import re
import shutil
from pathlib import PurePosixPath

import pytest

from tests.lifecycle.scenarios._kit import (
    Known,
    Scenario,
    Tree,
    assert_changes,
    credits,
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
    """T-6b ``pair|PRIM=lsp|DEPLOYED=unowned`` (#4628): install keeps the user's entry."""
    scenario, tree = _start(lifecycle_world, ("p-lsp",))
    servers = {"lspServers": {LSP_SERVER: {"command": "mine"}, "user-lsp": {"command": "u"}}}
    scenario.user_write(".github/lsp.json", json.dumps(servers, indent=2) + "\n")
    scenario.run("install p-lsp")
    scenario.run("approve p-lsp")

    # The first failing install still commits the dependency row it resolved, as
    # the MCP native-conflict path does (#4282); the rerun changes nothing.
    known = {"I7": Known("#4282", r"failed install changed the lockfile")}
    for line, cells in (("install", known), ("install --force", None)):
        step = scenario.run(line, exit=1, known=cells)
        assert f"LSP server '{LSP_SERVER}' conflicts with the existing" in step.output, line
        assert "Preserved that entry" in step.output, line
        tree.assert_intact(line)
    assert_changes(step.changed, (), label="rerun")
    lock = scenario.lock_path.read_text(encoding="utf-8")
    assert LSP_SERVER not in lock, "the lock claims the user's entry"
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


def _author_exec(world, revision: str = "v1", *, lsp: str | None = None) -> dict[str, str]:
    """Author ``o-exec`` (hook + copied script, two-member canvas) at *revision*.

    ``o-lsp`` is authored at *lsp* (default: *revision*).
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
    icon = {PurePosixPath("icon.txt"): f"{revision}\n".encode()}
    factory.add_canvas(package, EDITED, extension, assets=icon)
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


_EDITED_CREDITS = tuple({"PRIM": kind, "DEPLOYED": "edited"} for kind in _EXEC_KINDS)


def test_edited_executable_files_are_kept(lifecycle_world) -> None:
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

    step = scenario.run("install", credit=_EDITED_CREDITS)
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
    {"test_edited_executable_files_are_kept": _EDITED_CREDITS},
)
