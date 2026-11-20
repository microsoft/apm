"""End-to-end native Agent Plugin lifecycle through the APM CLI (issue #2703)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.copilot_plugins.constants import (
    ENABLED_PLUGINS_KEY,
    EXTRA_MARKETPLACES_KEY,
)
from apm_cli.copilot_plugins.registrar import catalog_path_for, ledger_path_for
from apm_cli.deps.lockfile import LockFile
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.native_plugins.binding import project_marketplace_name, user_marketplace_name

from ._builders import (
    read_json,
    write_agent_plugin,
    write_legacy_package,
)

pytestmark = pytest.mark.component


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
    # In-process CLI runs share the apm.yml parse cache; drop the stale entry.
    clear_apm_yml_cache()


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


MARKETPLACE = project_marketplace_name("consumer")


def _settings_path(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.json"


def _local_settings_path(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.local.json"


def _modules(project: Path) -> Path:
    return project / "apm_modules"


def _plugins_root(root: Path) -> Path:
    return root / ".agents" / "plugins"


def _catalog_path(root: Path) -> Path:
    return _plugins_root(root) / ".claude-plugin" / "marketplace.json"


def _catalog_names(root: Path) -> list[str]:
    return [entry["name"] for entry in read_json(_catalog_path(root))["plugins"]]


def _assert_no_legacy_registration(project: Path) -> None:
    assert not catalog_path_for(_modules(project)).exists()
    assert not ledger_path_for(_modules(project)).exists()
    assert not _local_settings_path(project).exists()


def test_offline_install_registers_the_plugin_without_decomposing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Target admission installs the whole unit without inspecting a runtime."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    materialized = _modules(project) / "_local" / "sentinel"
    assert (materialized / "plugin.json").is_file()
    assert (materialized / "skills" / "sentinel-skill" / "SKILL.md").is_file()
    assert (materialized / "mcp.json").is_file()

    deployed = _plugins_root(project) / "sentinel"
    assert (deployed / "plugin.json").read_bytes() == (materialized / "plugin.json").read_bytes()
    assert (deployed / "skills" / "sentinel-skill" / "SKILL.md").is_file()
    assert (deployed / "mcp.json").is_file()

    assert read_json(_catalog_path(project)) == {
        "name": MARKETPLACE,
        "owner": {"name": "APM"},
        "plugins": [{"name": "sentinel", "source": "./sentinel"}],
    }
    settings = read_json(_settings_path(project))
    assert settings == {
        EXTRA_MARKETPLACES_KEY: {
            MARKETPLACE: {"source": {"source": "directory", "path": "./.agents/plugins"}}
        },
        ENABLED_PLUGINS_KEY: {f"sentinel@{MARKETPLACE}": True},
    }
    _assert_no_legacy_registration(project)

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
    assert not _plugins_root(project).exists()
    assert not _settings_path(project).exists()
    _assert_no_legacy_registration(project)
    assert not (project / ".agents" / "skills" / "sentinel-skill").exists()
    assert not (project / ".github" / "skills" / "sentinel-skill").exists()
    assert not (project / ".github" / "mcp.json").exists()


def test_mixed_graph_registers_the_plugin_and_deploys_the_legacy_package(
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
    assert _catalog_names(project) == ["sentinel"]
    assert not (_plugins_root(project) / "classic").exists()
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


def test_legacy_parent_integrates_while_transitive_plugin_registers_opaquely(
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
    assert _catalog_names(project) == ["transitive-plugin"]
    assert (_plugins_root(project) / "transitive-plugin" / "plugin.json").is_file()
    assert read_json(_settings_path(project))[ENABLED_PLUGINS_KEY] == {
        f"transitive-plugin@{MARKETPLACE}": True
    }
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
    assert not _plugins_root(project).exists()
    assert not _settings_path(project).exists()
    assert not (project / ".claude" / "settings.json").exists()
    _assert_no_legacy_registration(project)
    assert not (project / ".claude" / "skills" / "transitive-plugin-skill").exists()
    assert not (project / ".agents" / "skills" / "transitive-plugin-skill").exists()


def test_reinstall_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore/update rebuilds byte-identical registration state."""
    project = tmp_path / "project"
    write_agent_plugin(tmp_path / "source" / "sentinel", name="sentinel")
    _write_project(project, [str(tmp_path / "source" / "sentinel")])

    assert _install(monkeypatch, project).exit_code == 0
    catalog_first = _catalog_path(project).read_bytes()
    settings_first = _settings_path(project).read_bytes()
    lock_first = (project / "apm.lock.yaml").read_bytes()

    assert _install(monkeypatch, project).exit_code == 0

    assert _catalog_path(project).read_bytes() == catalog_first
    assert _settings_path(project).read_bytes() == settings_first
    assert (project / "apm.lock.yaml").read_bytes() == lock_first


def test_source_edits_propagate_to_the_deployed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apm install --force refreshes the deployed copy Copilot loads."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel", skill_body="MARK_V1")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    write_agent_plugin(source, name="sentinel", skill_body="MARK_V2")
    assert _install(monkeypatch, project, "--force").exit_code == 0

    live = _plugins_root(project) / "sentinel" / "skills" / "sentinel-skill" / "SKILL.md"
    assert "MARK_V2" in live.read_text(encoding="utf-8")
    assert _catalog_names(project) == ["sentinel"]


def test_uninstall_removes_only_apm_owned_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstall retires APM's rows and never deletes user bytes."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    settings_path = _settings_path(project)
    document = read_json(settings_path)
    document["banner"] = "keep me"
    document[ENABLED_PLUGINS_KEY]["mine@elsewhere"] = False
    settings_path.write_text(json.dumps(document, indent=2), encoding="ascii")

    result = CliRunner().invoke(cli, ["uninstall", str(source)], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert read_json(settings_path) == {
        "banner": "keep me",
        EXTRA_MARKETPLACES_KEY: {},
        ENABLED_PLUGINS_KEY: {"mine@elsewhere": False},
    }
    assert not _plugins_root(project).exists()
    _assert_no_legacy_registration(project)
    assert (source / "plugin.json").is_file()


def test_compile_does_not_rewrite_the_native_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apm compile stays non-destructive for a natively registered plugin."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    roots = (_modules(project) / "_local" / "sentinel", _plugins_root(project))

    def snapshot() -> list[dict[str, bytes]]:
        return [
            {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
            for root in roots
        ]

    before = snapshot()

    CliRunner().invoke(cli, ["compile"], catch_exceptions=False)

    assert snapshot() == before


def test_global_scope_registers_in_user_copilot_settings(
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
    marketplace = user_marketplace_name(home / ".apm")
    settings = read_json(copilot_home / "settings.json")
    assert settings == {
        EXTRA_MARKETPLACES_KEY: {
            marketplace: {
                "source": {"source": "directory", "path": str(_plugins_root(home))},
            }
        },
        ENABLED_PLUGINS_KEY: {f"sentinel@{marketplace}": True},
    }
    assert _catalog_names(home) == ["sentinel"]
    assert (_plugins_root(home) / "sentinel" / "plugin.json").is_file()
    assert not catalog_path_for(home / ".apm" / "apm_modules").exists()
    # A global registration must never leak into the working directory.
    assert not (elsewhere / ".github").exists()
    assert not (elsewhere / ".agents").exists()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing: prune's package scan (commands/deps/_utils."
        "_scan_installed_packages) does not recognize a plugin.json-only Agent "
        "Plugin root, so an orphaned Agent Plugin is never pruned."
    ),
)
def test_prune_retires_the_registration_for_an_orphaned_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prune retires the declaration and deployed root of a plugin it prunes."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    settings_path = _settings_path(project)
    document = read_json(settings_path)
    document["banner"] = "keep me"
    settings_path.write_text(json.dumps(document), encoding="ascii")
    _write_project(project, [])

    result = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert read_json(settings_path) == {
        "banner": "keep me",
        EXTRA_MARKETPLACES_KEY: {},
        ENABLED_PLUGINS_KEY: {},
    }
    assert not _plugins_root(project).exists()
    assert not (_modules(project) / "_local" / "sentinel").exists()
    assert (source / "plugin.json").is_file()


def test_project_registration_contains_no_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relocated clone or worktree keeps working: nothing is host-anchored."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    catalog_text = _catalog_path(project).read_text(encoding="utf-8")
    settings_text = _settings_path(project).read_text(encoding="utf-8")

    for text in (catalog_text, settings_text):
        assert str(tmp_path) not in text
        assert '"/' not in text.replace('"./', '"')


def test_install_root_redirect_writes_the_registration_under_that_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--root`` relocates the whole registration, not just apm_modules."""
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
    assert _catalog_names(target_root) == ["sentinel"]
    assert read_json(_settings_path(target_root))[ENABLED_PLUGINS_KEY] == {
        f"sentinel@{MARKETPLACE}": True
    }
    assert not _settings_path(workspace).exists()
    assert not _plugins_root(workspace).exists()


def test_install_moves_a_legacy_registration_to_the_native_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrading retires the old ``apm`` entries and declares the deployed root."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    legacy_files = {
        catalog_path_for(_modules(project)): {
            "name": "apm",
            "plugins": [{"name": "sentinel", "source": "./_local/sentinel"}],
        },
        ledger_path_for(_modules(project)): {
            "version": 1,
            "marketplace": "apm",
            "marketplaceOwned": True,
            "marketplacePath": "apm_modules",
            "settingsPath": ".github/copilot/settings.local.json",
            "enabledPlugins": ["sentinel@apm"],
        },
        _local_settings_path(project): {
            "model": "keep-me",
            EXTRA_MARKETPLACES_KEY: {
                "apm": {"source": {"source": "directory", "path": "apm_modules"}}
            },
            ENABLED_PLUGINS_KEY: {"sentinel@apm": True},
        },
    }
    for path, document in legacy_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="ascii")

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    assert read_json(_local_settings_path(project)) == {"model": "keep-me"}
    assert not catalog_path_for(_modules(project)).exists()
    assert not ledger_path_for(_modules(project)).exists()
    assert read_json(_settings_path(project))[ENABLED_PLUGINS_KEY] == {
        f"sentinel@{MARKETPLACE}": True
    }
    assert _catalog_names(project) == ["sentinel"]
