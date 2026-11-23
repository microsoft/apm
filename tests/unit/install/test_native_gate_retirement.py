"""Gate retirement judges a locked native plugin as install's admission does (#3921)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.claude_plugins.admission import (
    fingerprint_native_claude_source,
    locked_native_retired,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.native_plugins.record import NativePluginRecord

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I4")]

_KEY = "acme/tool"


def _lock(modules: Path) -> LockFile:
    root = modules / "acme/tool"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "tool"}))
    (root / "bin").mkdir()
    (root / "bin/tool").write_text("#!/bin/sh\n")
    dependency = LockedDependency(
        repo_url=_KEY, resolved_commit="a" * 40, package_type="marketplace_plugin"
    )
    dependency.native_plugin = NativePluginRecord(
        "tool", "sha256:" + "0" * 64, "apm-p-00000000", "./.agents/plugins"
    )
    return LockFile(dependencies={_KEY: dependency})


def _policy(**bin_deploy) -> SimpleNamespace:
    rules = SimpleNamespace(**{"deny_all": False, "deny": (), **bin_deploy})
    return SimpleNamespace(bin_deploy=rules, mcp=None, enforcement="block")


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (_policy(), {}),
        (
            _policy(deny_all=True),
            {_KEY: "org policy bin_deploy.deny_all blocks its bin/ executables"},
        ),
        (
            _policy(deny=(_KEY,)),
            {_KEY: "org policy bin_deploy.deny blocks its bin/ executables"},
        ),
    ],
    ids=["allowed", "deny-all", "deny-package"],
)
def test_an_org_bin_deploy_deny_retires_a_native_plugin_that_ships_bin(
    tmp_path: Path, policy: SimpleNamespace, expected: dict[str, str]
) -> None:
    """With the gate off, only the org bin_deploy rule decides, as install's park does."""
    modules = tmp_path / "apm_modules"
    retired = locked_native_retired(_lock(modules), modules, lambda: None, policy)
    assert retired == expected


@pytest.mark.parametrize(
    ("deployed", "expected"),
    [
        ("verified", {_KEY: "org policy bin_deploy.deny_all blocks its bin/ executables"}),
        (
            "changed",
            {
                _KEY: "its apm_modules source is missing and the deployed copy differs from "
                "apm.lock.yaml, so its approval cannot be checked"
            },
        ),
        ("absent", {}),
    ],
)
def test_a_missing_source_is_judged_on_the_verified_deployed_root(
    tmp_path: Path, deployed: str, expected: dict[str, str]
) -> None:
    """Without apm_modules, prune and update judge the deployed root or fail closed."""
    import shutil

    modules = tmp_path / "apm_modules"
    lock = _lock(modules)
    root = tmp_path / ".agents/plugins/tool"
    if deployed != "absent":
        shutil.copytree(modules / "acme/tool", root)
        lock.dependencies[_KEY].native_plugin = NativePluginRecord(
            "tool",
            fingerprint_native_claude_source(root, portable=True),
            "apm-p-00000000",
            "./.agents/plugins",
        )
    if deployed == "changed":
        (root / "bin/tool").write_text("#!/bin/sh\necho changed\n")
    shutil.rmtree(modules)
    retired = locked_native_retired(lock, modules, lambda: None, _policy(deny_all=True))
    assert retired == expected


@pytest.mark.parametrize(
    ("transitive", "allow", "refused"),
    [(True, None, True), (False, None, False), (True, ("srv",), False)],
    ids=["transitive", "direct", "transitive-listed"],
)
def test_a_native_plugins_mcp_server_meets_org_trust_transitive(
    tmp_path: Path, transitive: bool, allow: tuple[str, ...] | None, refused: bool
) -> None:
    """A transitive native plugin's server needs mcp.allow, as a loose one does (req-sc-016)."""
    from apm_cli.claude_plugins.admission import native_policy_violation
    from apm_cli.policy.schema import ApmPolicy, McpPolicy

    root = tmp_path / "tool"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "tool", "mcpServers": {"srv": {"command": "npx", "args": ["x"]}}})
    )
    mcp = McpPolicy(deny=("unrelated",)) if allow is None else McpPolicy(allow=allow)
    policy = ApmPolicy(enforcement="block", mcp=mcp)
    violation = native_policy_violation(root, policy, transitive=transitive)
    assert (violation is not None and "mcp-trust-transitive" in violation) is refused


@pytest.mark.parametrize(
    ("transitive", "refused"), [(True, True), (False, False)], ids=["transitive", "direct"]
)
def test_an_unreadable_native_mcp_bundle_meets_org_trust_transitive(
    tmp_path: Path, transitive: bool, refused: bool
) -> None:
    """A bundle APM cannot read never shows it is in mcp.allow (req-sc-016)."""
    from apm_cli.claude_plugins.admission import native_policy_violation
    from apm_cli.policy.schema import ApmPolicy, McpPolicy

    root = tmp_path / "tool"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "tool", "mcpServers": "./bundle.dxt"})
    )
    (root / "bundle.dxt").write_bytes(b"PK")
    policy = ApmPolicy(enforcement="block", mcp=McpPolicy(trust_transitive=False))
    violation = native_policy_violation(root, policy, transitive=transitive)
    assert (violation is not None) is refused


def _deployed_project(tmp_path: Path, names: tuple[str, ...]) -> tuple[Path, Path, LockFile]:
    """A project whose native plugins *names* are deployed and declared."""
    from apm_cli.claude_plugins.registrar import reconcile_native_plugins, scope_binding
    from apm_cli.core.scope import InstallScope

    project = tmp_path / "project"
    modules = project / "apm_modules"
    project.mkdir()
    (project / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    binding = scope_binding(
        user_scope=False, modules_dir=modules, deploy_root=project, previous=None
    )
    lock = LockFile()
    for name in names:
        root = modules / "acme" / name
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
        dependency = LockedDependency(
            repo_url=f"acme/{name}", resolved_commit="a" * 40, package_type="marketplace_plugin"
        )
        dependency.native_plugin = NativePluginRecord(
            name, fingerprint_native_claude_source(root, portable=True), *binding
        )
        lock.dependencies[f"acme/{name}"] = dependency
    reconcile_native_plugins(
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        previous=None,
        lockfile=lock,
        deploy_root=project,
    )
    return project, modules, lock


def _settings_bytes(project: Path) -> dict[str, bytes | None]:
    from apm_cli.native_plugins.scopes import PROJECT_SETTINGS_RELATIVE

    return {
        relative: (project / relative).read_bytes() if (project / relative).exists() else None
        for relative in PROJECT_SETTINGS_RELATIVE.values()
    }


@pytest.mark.parametrize(
    ("names", "user_edit", "removed"),
    [
        (("tool",), None, ("acme/tool",)),
        (("tool",), "drop-entry", ("acme/tool",)),
        (("tool", "other"), None, ("acme/tool",)),
        (("tool", "other"), "set-false", ("acme/tool",)),
        (("tool", "other"), "set-false", ("acme/tool", "acme/other")),
    ],
    ids=["last", "last-entry-absent", "survivor", "survivor-user-false", "orphan-user-false"],
)
def test_the_settings_preview_names_exactly_the_files_retirement_changes(
    tmp_path: Path, names: tuple[str, ...], user_edit: str | None, removed: tuple[str, ...]
) -> None:
    """#4754: the prune --dry-run settings preview matches what the reconcile writes."""
    import copy

    from apm_cli.claude_plugins.registrar import (
        native_ownership,
        preview_project_settings_changes,
        reconcile_native_plugins,
    )
    from apm_cli.core.scope import InstallScope

    project, modules, lock = _deployed_project(tmp_path, names)
    claude = project / ".claude/settings.json"
    if user_edit is not None:
        document = json.loads(claude.read_text(encoding="utf-8"))
        entry = next(key for key in document["enabledPlugins"] if key.startswith("tool@"))
        if user_edit == "drop-entry":
            del document["enabledPlugins"][entry]
        else:
            document["enabledPlugins"][entry] = False
        claude.write_text(json.dumps(document, indent=2), encoding="utf-8")
    previous = native_ownership(lock)
    following = copy.deepcopy(lock)
    # A gate retirement drops the record; an orphan prune drops the whole row.
    following.dependencies["acme/tool"].native_plugin = None
    for key in removed[1:]:
        del following.dependencies[key]
    before = _settings_bytes(project)

    preview = preview_project_settings_changes(
        modules_dir=modules, previous=previous, lockfile=following, deploy_root=project
    )
    assert _settings_bytes(project) == before, "the preview writes nothing"
    reconcile_native_plugins(
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        previous=previous,
        lockfile=following,
        deploy_root=project,
    )
    after = _settings_bytes(project)

    assert set(preview) == {path for path, data in before.items() if after[path] != data}
    assert preview


def test_the_settings_preview_refuses_a_moved_marketplace_binding(tmp_path: Path) -> None:
    """#4754: a binding conflict the reconcile refuses is a preview error, not a change."""
    import copy

    from apm_cli.claude_plugins.registrar import (
        native_ownership,
        preview_project_settings_changes,
    )
    from apm_cli.native_plugins.settings import NativeSettingsError

    project, modules, lock = _deployed_project(tmp_path, ("tool", "other"))
    claude = project / ".claude/settings.json"
    document = json.loads(claude.read_text(encoding="utf-8"))
    for entry in document["extraKnownMarketplaces"].values():
        entry["source"] = {"source": "directory", "path": "./elsewhere"}
    claude.write_text(json.dumps(document, indent=2), encoding="utf-8")
    following = copy.deepcopy(lock)
    following.dependencies["acme/tool"].native_plugin = None

    with pytest.raises(NativeSettingsError):
        preview_project_settings_changes(
            modules_dir=modules,
            previous=native_ownership(lock),
            lockfile=following,
            deploy_root=project,
        )
