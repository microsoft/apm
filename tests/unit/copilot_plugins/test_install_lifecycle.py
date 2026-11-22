"""End-to-end native Agent Plugin lifecycle through the APM CLI (#2703, #3921)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.native_plugins.record import lock_native_plugins

from ._builders import (
    copilot_project_settings,
    legacy_copilot_settings,
    native_catalog_path,
    native_marketplace,
    native_plugins_root,
    read_json,
    write_agent_plugin,
    write_legacy_package,
)

pytestmark = pytest.mark.component

_MARKETPLACES = "extraKnownMarketplaces"
_ENABLED = "enabledPlugins"


def _write_project(project: Path, dependencies: list[str]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "description": "consumer",
                "dependencies": {"apm": dependencies},
            }
        ),
        encoding="ascii",
    )


def _install(monkeypatch: pytest.MonkeyPatch, project: Path, *args: str):
    return _install_for_target(monkeypatch, project, "copilot", *args)


def _install_for_target(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    target: str,
    *args: str,
):
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        cli,
        ["install", "--no-policy", "--target", target, *args],
        catch_exceptions=False,
    )


def _modules(project: Path) -> Path:
    return project / "apm_modules"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_no_native_state(project: Path) -> None:
    assert not native_plugins_root(project).exists()
    assert not copilot_project_settings(project).exists()
    assert not legacy_copilot_settings(project).exists()


def test_offline_install_activates_the_plugin_without_decomposing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Target admission deploys the whole unit without inspecting a runtime."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    deployed = native_plugins_root(project) / "sentinel"
    assert (deployed / "plugin.json").is_file()
    assert (deployed / "skills" / "sentinel-skill" / "SKILL.md").is_file()
    assert (deployed / "mcp.json").is_file()

    marketplace = native_marketplace(project)
    assert marketplace.startswith("apm-p-")
    assert read_json(native_catalog_path(project)) == {
        "name": marketplace,
        "owner": {"name": "APM"},
        "plugins": [{"name": "sentinel", "source": "./sentinel"}],
    }

    settings = read_json(copilot_project_settings(project))
    assert settings[_MARKETPLACES] == {
        marketplace: {"source": {"source": "directory", "path": "./.agents/plugins"}}
    }
    assert settings[_ENABLED] == {f"sentinel@{marketplace}": True}
    assert not legacy_copilot_settings(project).exists()

    lock = LockFile.read(project / "apm.lock.yaml")
    records = list(lock_native_plugins(lock).values())
    assert [(r.name, r.root, r.binding) for r in records] == [
        ("sentinel", ".agents/plugins/sentinel", (marketplace, "./.agents/plugins"))
    ]

    # The plugin is loaded natively, so APM must not also decompose it.
    assert not (project / ".agents" / "skills").exists()
    assert not (project / ".github" / "skills").exists()
    assert not (project / ".github" / "mcp.json").exists()


def test_no_private_copilot_state_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APM writes documented settings only -- never Copilot's private cache."""
    project = tmp_path / "project"
    copilot_home = tmp_path / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    write_agent_plugin(tmp_path / "source" / "sentinel", name="sentinel")
    _write_project(project, [str(tmp_path / "source" / "sentinel")])

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    assert not (copilot_home / "installed-plugins").exists()
    assert not (copilot_home / "config.json").exists()
    assert not (copilot_home / "settings.json").exists()


def test_non_copilot_target_plugin_noop_fails_without_committing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #2796: target exclusion cannot be a successful no-op.

    Admission is a pure function of resolved target names -- a project-level
    target set that excludes copilot is a per-package skip, not a structural
    package failure. When that skip leaves the command with nothing deployed,
    install fails loudly and commits no durable state.
    """
    project = tmp_path / "project"
    write_agent_plugin(tmp_path / "source" / "sentinel", name="sentinel")
    _write_project(project, [str(tmp_path / "source" / "sentinel")])
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        cli,
        ["install", "--no-policy", "--target", "claude"],
        catch_exceptions=False,
    )

    output = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert "'copilot' target" in output
    assert "No selected target received this package" in output
    assert "apm install" in output
    assert not (project / "apm.lock.yaml").exists()
    _assert_no_native_state(project)
    assert not (project / ".agents" / "skills" / "sentinel-skill").exists()
    assert not (project / ".github" / "skills" / "sentinel-skill").exists()
    assert not (project / ".github" / "mcp.json").exists()


def test_mixed_graph_activates_the_plugin_and_deploys_the_legacy_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy packages keep decomposing while plugins stay opaque."""
    project = tmp_path / "project"
    write_agent_plugin(tmp_path / "source" / "sentinel", name="sentinel")
    write_legacy_package(tmp_path / "source" / "classic", name="classic")
    _write_project(
        project,
        [str(tmp_path / "source" / "sentinel"), str(tmp_path / "source" / "classic")],
    )

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    catalog = read_json(native_catalog_path(project))
    assert [entry["name"] for entry in catalog["plugins"]] == ["sentinel"]
    assert (project / ".agents" / "skills" / "classic-skill" / "SKILL.md").is_file()


def _write_legacy_parent_with_transitive_plugin(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    child = write_agent_plugin(source / "portable-child", name="transitive-plugin")
    parent = write_legacy_package(
        source / "legacy-parent",
        name="legacy-parent",
        dependencies=["../portable-child"],
    )
    return parent, child


def test_legacy_parent_integrates_while_transitive_plugin_activates_opaquely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy parent and its native child keep distinct deployment semantics."""
    project = tmp_path / "project"
    parent, _ = _write_legacy_parent_with_transitive_plugin(tmp_path)
    _write_project(project, [str(parent)])

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    assert (project / ".agents" / "skills" / "legacy-parent-skill" / "SKILL.md").is_file()
    catalog = read_json(native_catalog_path(project))
    assert catalog["plugins"] == [{"name": "transitive-plugin", "source": "./transitive-plugin"}]
    assert (native_plugins_root(project) / "transitive-plugin" / "plugin.json").is_file()
    assert not (project / ".agents" / "skills" / "transitive-plugin-skill").exists()
    assert not (project / ".github" / "skills" / "transitive-plugin-skill").exists()
    assert not (project / ".github" / "mcp.json").exists()

    lock = LockFile.from_yaml((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    locked = lock.get_package_dependencies()
    assert [(dep.package_type, dep.depth) for dep in locked] == [
        ("apm_package", 1),
        ("agent_plugin", 2),
    ]
    assert locked[1].declaring_parent is not None


def test_excluded_target_keeps_legacy_parent_and_transitive_plugin_opaque(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target exclusion keeps the graph but creates no plugin projection."""
    project = tmp_path / "project"
    parent, _ = _write_legacy_parent_with_transitive_plugin(tmp_path)
    _write_project(project, [str(parent)])

    result = _install_for_target(monkeypatch, project, "claude")

    assert result.exit_code == 0, result.output
    assert (project / ".claude" / "skills" / "legacy-parent-skill" / "SKILL.md").is_file()
    lock = LockFile.from_yaml((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    locked = lock.get_package_dependencies()
    assert [(dep.package_type, dep.depth) for dep in locked] == [
        ("apm_package", 1),
        ("agent_plugin", 2),
    ]
    child = next(dep for dep in locked if dep.package_type == "agent_plugin")
    assert child.declaring_parent is not None
    assert child.native_plugin is None
    _assert_no_native_state(project)
    assert not (project / ".claude" / "skills" / "transitive-plugin-skill").exists()
    assert not (project / ".agents" / "skills" / "transitive-plugin-skill").exists()


def test_reinstall_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore/update rebuilds byte-identical activation state."""
    project = tmp_path / "project"
    write_agent_plugin(tmp_path / "source" / "sentinel", name="sentinel")
    _write_project(project, [str(tmp_path / "source" / "sentinel")])

    assert _install(monkeypatch, project).exit_code == 0
    deployed_first = _tree(native_plugins_root(project))
    settings_first = copilot_project_settings(project).read_bytes()

    assert _install(monkeypatch, project).exit_code == 0

    assert _tree(native_plugins_root(project)) == deployed_first
    assert copilot_project_settings(project).read_bytes() == settings_first


def test_source_edits_reach_the_deployed_copy_on_the_next_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed root is a copy: a source edit lands on the next install."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel", skill_body="MARK_V1")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    deployed = native_plugins_root(project) / "sentinel" / "skills" / "sentinel-skill" / "SKILL.md"

    write_agent_plugin(source, name="sentinel", skill_body="MARK_V2")
    assert "MARK_V1" in deployed.read_text(encoding="utf-8")
    result = _install(monkeypatch, project, "--force")

    assert result.exit_code == 0, result.output
    assert "MARK_V2" in deployed.read_text(encoding="utf-8")
    catalog = read_json(native_catalog_path(project))
    assert catalog["plugins"] == [{"name": "sentinel", "source": "./sentinel"}]


def test_uninstall_removes_only_apm_owned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstall retires APM's keys and roots and never deletes user bytes."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    settings_path = copilot_project_settings(project)
    document = read_json(settings_path)
    document["banner"] = "keep me"
    team = {"source": {"source": "directory", "path": "team"}}
    document[_MARKETPLACES]["team"] = team
    document[_ENABLED]["team-plugin@team"] = True
    settings_path.write_text(json.dumps(document, indent=2), encoding="ascii")

    result = CliRunner().invoke(cli, ["uninstall", str(source)], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    settings = read_json(settings_path)
    assert settings["banner"] == "keep me"
    assert settings[_MARKETPLACES] == {"team": team}
    assert settings[_ENABLED] == {"team-plugin@team": True}
    assert not native_plugins_root(project).exists()
    assert (source / "plugin.json").is_file()


def test_compile_does_not_rewrite_the_native_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apm compile stays non-destructive for a natively activated plugin."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    materialized = _modules(project) / "_local" / "sentinel"
    deployed = native_plugins_root(project)
    before = (_tree(materialized), _tree(deployed))

    CliRunner().invoke(cli, ["compile"], catch_exceptions=False)

    assert (_tree(materialized), _tree(deployed)) == before


def test_global_scope_declares_in_user_copilot_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A global install is available from any directory, not one repository."""
    home = tmp_path / "home"
    (home / ".apm").mkdir(parents=True)
    copilot_home = home / ".copilot"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    (home / ".apm" / "apm.yml").write_text(
        json.dumps(
            {
                "name": "user",
                "version": "1.0.0",
                "description": "user",
                "dependencies": {"apm": [str(source)]},
            }
        ),
        encoding="ascii",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = CliRunner().invoke(
        cli,
        ["install", "-g", "--no-policy", "--target", "copilot"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    marketplace = native_marketplace(home)
    assert marketplace.startswith("apm-u-")
    settings = read_json(copilot_home / "settings.json")
    assert settings[_ENABLED] == {f"sentinel@{marketplace}": True}
    written = settings[_MARKETPLACES][marketplace]["source"]
    assert written["source"] == "directory"
    assert Path(written["path"]).is_absolute()
    assert Path(written["path"]) == native_plugins_root(home)
    assert (native_plugins_root(home) / "sentinel" / "plugin.json").is_file()
    # A global activation must never leak into the working directory.
    assert not (elsewhere / ".github").exists()
    assert not (elsewhere / ".agents").exists()


def test_prune_retires_the_activation_of_an_orphaned_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prune removes only APM-owned entries for packages it prunes."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    # prune's orphan scan recognizes package roots by apm.yml; plugin.json
    # still owns the identity, and the deployed copy drops the root apm.yml.
    (source / "apm.yml").write_text(
        json.dumps(
            {"name": "sentinel", "version": "1.0.0", "description": "Portable Agent Plugin fixture"}
        ),
        encoding="ascii",
    )
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    marketplace = native_marketplace(project)
    assert not (native_plugins_root(project) / "sentinel" / "apm.yml").exists()
    _write_project(project, [])
    clear_apm_yml_cache()

    result = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    settings = read_json(copilot_project_settings(project))
    assert f"sentinel@{marketplace}" not in settings.get(_ENABLED, {})
    assert marketplace not in settings.get(_MARKETPLACES, {})
    assert not native_plugins_root(project).exists()
    assert (source / "plugin.json").is_file()


def test_project_activation_contains_no_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relocated clone or worktree keeps working: nothing is host-anchored."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    catalog_text = native_catalog_path(project).read_text(encoding="utf-8")
    settings_text = copilot_project_settings(project).read_text(encoding="utf-8")

    for text in (catalog_text, settings_text):
        assert str(tmp_path) not in text
        assert '"/' not in text.replace('"./', '"')


def test_install_root_redirect_writes_the_activation_under_that_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--root`` relocates the whole activation, not just apm_modules."""
    workspace = tmp_path / "workspace"
    target_root = tmp_path / "target-root"
    workspace.mkdir()
    target_root.mkdir()
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(workspace, [str(source)])
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(
        cli,
        ["install", "--no-policy", "--target", "copilot", "--root", str(target_root)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (native_plugins_root(target_root) / "sentinel" / "plugin.json").is_file()
    marketplace = native_marketplace(target_root)
    assert read_json(copilot_project_settings(target_root))[_ENABLED] == {
        f"sentinel@{marketplace}": True
    }
    _assert_no_native_state(workspace)


def test_install_migrates_a_legacy_apm_marketplace_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The removed ``apm`` marketplace route is retired; user entries are kept."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    legacy_dir = _modules(project) / ".github" / "plugin"
    legacy_dir.mkdir(parents=True)
    ledger = legacy_dir / "apm-registration.json"
    catalog = legacy_dir / "marketplace.json"
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "marketplace": "apm",
                "marketplaceOwned": True,
                "marketplacePath": "apm_modules",
                "settingsPath": ".github/copilot/settings.local.json",
                "enabledPlugins": ["sentinel@apm"],
            }
        ),
        encoding="ascii",
    )
    catalog.write_text(
        json.dumps({"name": "apm", "plugins": [{"name": "sentinel", "source": "./x"}]}),
        encoding="ascii",
    )
    team = {"source": {"source": "directory", "path": "team"}}
    legacy_settings = legacy_copilot_settings(project)
    legacy_settings.parent.mkdir(parents=True)
    legacy_settings.write_text(
        json.dumps(
            {
                "banner": "keep me",
                _MARKETPLACES: {
                    "apm": {"source": {"source": "directory", "path": "apm_modules"}},
                    "team": team,
                },
                _ENABLED: {
                    "sentinel@apm": True,
                    "disabled@apm": False,
                    "team-plugin@team": True,
                },
            }
        ),
        encoding="ascii",
    )

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    assert read_json(legacy_settings) == {
        "banner": "keep me",
        _MARKETPLACES: {"team": team},
        _ENABLED: {"disabled@apm": False, "team-plugin@team": True},
    }
    assert not ledger.exists()
    assert not catalog.exists()
    marketplace = native_marketplace(project)
    assert read_json(copilot_project_settings(project))[_ENABLED] == {
        f"sentinel@{marketplace}": True
    }
