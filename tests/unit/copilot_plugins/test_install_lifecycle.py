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


def _settings_path(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.local.json"


def _modules(project: Path) -> Path:
    return project / "apm_modules"


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

    catalog = read_json(catalog_path_for(_modules(project)))
    assert catalog["plugins"] == [
        {
            "name": "sentinel",
            "source": "./_local/sentinel",
            "version": "1.0.0",
            "description": "Portable Agent Plugin fixture",
        }
    ]

    settings = read_json(_settings_path(project))
    assert settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"] == {
        "source": "directory",
        "path": "apm_modules",
    }
    assert settings[ENABLED_PLUGINS_KEY] == {"sentinel@apm": True}

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


def test_non_copilot_target_skips_the_plugin_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project that does not target copilot skips the plugin, exit 0.

    Admission is a pure function of resolved target names -- a project-level
    target set that excludes copilot is a non-applicability, not a failure. It
    is skipped with one warning and the install still succeeds (Item 4). The
    fatal path is reserved for a package that WAS selected for copilot but
    cannot be registered.
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
    assert result.exit_code == 0, result.output
    assert "'copilot' target" in output
    materialized = _modules(project) / "_local" / "sentinel"
    assert (materialized / "plugin.json").is_file()
    lock = LockFile.from_yaml((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    locked = lock.get_package_dependencies()
    assert len(locked) == 1
    assert locked[0].package_type == "agent_plugin"
    assert not catalog_path_for(_modules(project)).exists()
    assert not ledger_path_for(_modules(project)).exists()
    assert not _settings_path(project).exists()
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
    catalog = read_json(catalog_path_for(_modules(project)))
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
    catalog = read_json(catalog_path_for(_modules(project)))
    assert [entry["name"] for entry in catalog["plugins"]] == ["transitive-plugin"]
    source = catalog["plugins"][0]["source"]
    child_root = _modules(project) / source.removeprefix("./")
    assert (child_root / "plugin.json").is_file()
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
    assert not catalog_path_for(_modules(project)).exists()
    assert not ledger_path_for(_modules(project)).exists()
    assert not _settings_path(project).exists()
    assert not (project / ".claude" / "skills" / "transitive-plugin-skill").exists()
    assert not (project / ".agents" / "skills" / "transitive-plugin-skill").exists()


def test_reinstall_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore/update rebuilds byte-identical registration state."""
    project = tmp_path / "project"
    write_agent_plugin(tmp_path / "source" / "sentinel", name="sentinel")
    _write_project(project, [str(tmp_path / "source" / "sentinel")])

    assert _install(monkeypatch, project).exit_code == 0
    catalog_first = catalog_path_for(_modules(project)).read_bytes()
    settings_first = _settings_path(project).read_bytes()

    assert _install(monkeypatch, project).exit_code == 0

    assert catalog_path_for(_modules(project)).read_bytes() == catalog_first
    assert _settings_path(project).read_bytes() == settings_first


def test_source_edits_propagate_without_recopying_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apm update refreshes the live bytes Copilot already points at."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel", skill_body="MARK_V1")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    write_agent_plugin(source, name="sentinel", skill_body="MARK_V2")
    assert _install(monkeypatch, project, "--force").exit_code == 0

    live = _modules(project) / "_local" / "sentinel" / "skills" / "sentinel-skill" / "SKILL.md"
    catalog = read_json(catalog_path_for(_modules(project)))
    assert "MARK_V2" in live.read_text(encoding="utf-8")
    assert catalog["plugins"][0]["source"] == "./_local/sentinel"


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
    settings_path.write_text(json.dumps(document, indent=2), encoding="ascii")

    result = CliRunner().invoke(cli, ["uninstall", str(source)], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    settings = read_json(settings_path)
    assert settings["banner"] == "keep me"
    assert EXTRA_MARKETPLACES_KEY not in settings
    assert ENABLED_PLUGINS_KEY not in settings
    assert not catalog_path_for(_modules(project)).exists()
    assert not ledger_path_for(_modules(project)).exists()
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
    materialized = _modules(project) / "_local" / "sentinel"
    before = {
        path.relative_to(materialized).as_posix(): path.read_bytes()
        for path in sorted(materialized.rglob("*"))
        if path.is_file()
    }

    CliRunner().invoke(cli, ["compile"], catch_exceptions=False)

    after = {
        path.relative_to(materialized).as_posix(): path.read_bytes()
        for path in sorted(materialized.rglob("*"))
        if path.is_file()
    }
    assert after == before


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
    user_modules = home / ".apm" / "apm_modules"
    settings = read_json(copilot_home / "settings.json")
    assert settings[ENABLED_PLUGINS_KEY] == {"sentinel@apm": True}
    assert settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"]["path"] == user_modules.as_posix()
    assert catalog_path_for(user_modules).is_file()
    # A global registration must never leak into the working directory.
    assert not (elsewhere / ".github").exists()


def test_prune_retires_the_registration_for_an_orphaned_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prune removes only APM-owned rows for packages it prunes."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    _write_project(project, [])
    (project / "apm.lock.yaml").unlink()

    result = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    settings = read_json(_settings_path(project))
    assert ENABLED_PLUGINS_KEY not in settings
    assert not catalog_path_for(_modules(project)).exists()
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

    catalog_text = catalog_path_for(_modules(project)).read_text(encoding="utf-8")
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
    assert catalog_path_for(_modules(target_root)).is_file()
    assert read_json(_settings_path(target_root))[ENABLED_PLUGINS_KEY] == {"sentinel@apm": True}
    assert not _settings_path(workspace).exists()
