"""Retiring the legacy ``apm`` Copilot registration (issue #3921).

Agent Plugins are now declared by the shared native reconcile. What remains in
``copilot_plugins.registrar`` removes exactly what a legacy
``apm-registration.json`` ledger proves APM wrote, then the ledger and catalog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.copilot_plugins.constants import ENABLED_PLUGINS_KEY, EXTRA_MARKETPLACES_KEY
from apm_cli.copilot_plugins.registrar import (
    catalog_path_for,
    ledger_path_for,
    registration_status,
    resync_native_plugins,
    synchronize_copilot_plugins,
)
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
from apm_cli.native_plugins.record import NativePluginRecord

from ._builders import read_json

pytestmark = pytest.mark.component

_APM_MARKETPLACE = {"source": {"source": "directory", "path": "apm_modules"}}
_USER_MARKETPLACE = {"source": {"source": "directory", "path": "/opt/mine"}}


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str, symbol: str | None = None) -> None:
        self.messages.append(message)

    def dry_run_notice(self, message: str) -> None:
        self.messages.append(message)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    modules = project / "apm_modules"
    modules.mkdir(parents=True)
    return project, modules


def _local_settings(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.local.json"


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="ascii")


def _write_legacy(
    modules: Path,
    *,
    plugins: list[str],
    marketplace_owned: bool = True,
    settings_path: str = ".github/copilot/settings.local.json",
) -> None:
    """Materialize the catalog + ledger the pre-#3921 route generated."""
    _write_json(
        catalog_path_for(modules),
        {
            "name": "apm",
            "plugins": [{"name": name, "source": f"./_local/{name}"} for name in plugins],
        },
    )
    _write_json(
        ledger_path_for(modules),
        {
            "version": 1,
            "marketplace": "apm",
            "marketplaceOwned": marketplace_owned,
            "marketplacePath": "apm_modules",
            "settingsPath": settings_path,
            "enabledPlugins": [f"{name}@apm" for name in plugins],
        },
    )


def _sync(project: Path, modules: Path, **kwargs) -> bool:
    return synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=kwargs.pop("scope", InstallScope.PROJECT),
        **kwargs,
    )


@pytest.mark.parametrize("entry", [synchronize_copilot_plugins, resync_native_plugins])
def test_migration_removes_exactly_the_ledger_proven_entries(tmp_path: Path, entry) -> None:
    """Only the ``apm`` marketplace and keys the ledger names are removed."""
    project, modules = _project(tmp_path)
    _write_legacy(modules, plugins=["sentinel"])
    settings = _local_settings(project)
    _write_json(
        settings,
        {
            "banner": "keep me",
            EXTRA_MARKETPLACES_KEY: {"apm": _APM_MARKETPLACE, "mine": _USER_MARKETPLACE},
            ENABLED_PLUGINS_KEY: {
                "sentinel@apm": True,
                "hand-added@apm": True,
                "tool@mine": False,
            },
        },
    )
    logger = _Logger()

    migrated = entry(
        project_root=project, modules_dir=modules, scope=InstallScope.PROJECT, logger=logger
    )

    assert migrated is True
    assert read_json(settings) == {
        "banner": "keep me",
        EXTRA_MARKETPLACES_KEY: {"mine": _USER_MARKETPLACE},
        ENABLED_PLUGINS_KEY: {"hand-added@apm": True, "tool@mine": False},
    }
    assert not catalog_path_for(modules).exists()
    assert not ledger_path_for(modules).exists()
    assert not (modules / ".github").exists()
    assert modules.is_dir()
    assert any(".agents/plugins" in message for message in logger.messages)


def test_migration_keeps_an_apm_marketplace_the_ledger_does_not_own(tmp_path: Path) -> None:
    project, modules = _project(tmp_path)
    _write_legacy(modules, plugins=["sentinel"], marketplace_owned=False)
    settings = _local_settings(project)
    _write_json(
        settings,
        {
            EXTRA_MARKETPLACES_KEY: {"apm": _USER_MARKETPLACE},
            ENABLED_PLUGINS_KEY: {"sentinel@apm": True},
        },
    )

    assert _sync(project, modules) is True

    assert read_json(settings) == {EXTRA_MARKETPLACES_KEY: {"apm": _USER_MARKETPLACE}}


def test_migration_deletes_a_settings_file_it_leaves_empty(tmp_path: Path) -> None:
    project, modules = _project(tmp_path)
    _write_legacy(modules, plugins=["sentinel"])
    settings = _local_settings(project)
    _write_json(
        settings,
        {
            EXTRA_MARKETPLACES_KEY: {"apm": _APM_MARKETPLACE},
            ENABLED_PLUGINS_KEY: {"sentinel@apm": True},
        },
    )

    assert _sync(project, modules) is True

    assert not settings.exists()


def test_migration_honors_the_ledger_recorded_shared_settings(tmp_path: Path) -> None:
    project, modules = _project(tmp_path)
    _write_legacy(modules, plugins=["sentinel"], settings_path=".github/copilot/settings.json")
    shared = project / ".github" / "copilot" / "settings.json"
    _write_json(
        shared,
        {
            "banner": "keep me",
            EXTRA_MARKETPLACES_KEY: {"apm": _APM_MARKETPLACE},
            ENABLED_PLUGINS_KEY: {"sentinel@apm": True},
        },
    )

    assert _sync(project, modules) is True

    assert read_json(shared) == {"banner": "keep me"}
    assert not _local_settings(project).exists()


def test_unreadable_ledger_removes_the_keys_its_catalog_lists(tmp_path: Path) -> None:
    """A corrupt ledger must not fail open: the catalog still names APM's keys."""
    project, modules = _project(tmp_path)
    _write_legacy(modules, plugins=["sentinel"])
    ledger_path_for(modules).write_text("{not json", encoding="ascii")
    settings = _local_settings(project)
    _write_json(
        settings,
        {
            EXTRA_MARKETPLACES_KEY: {"apm": _APM_MARKETPLACE, "mine": _USER_MARKETPLACE},
            ENABLED_PLUGINS_KEY: {"sentinel@apm": True, "tool@mine": True},
        },
    )

    assert _sync(project, modules) is True

    assert read_json(settings) == {
        EXTRA_MARKETPLACES_KEY: {"mine": _USER_MARKETPLACE},
        ENABLED_PLUGINS_KEY: {"tool@mine": True},
    }
    assert not ledger_path_for(modules).exists()
    assert not catalog_path_for(modules).exists()


def test_without_a_ledger_nothing_is_migrated(tmp_path: Path) -> None:
    """Ownership needs the ledger: a bare ``apm`` entry or catalog is not proof."""
    project, modules = _project(tmp_path)
    _write_json(catalog_path_for(modules), {"name": "apm", "plugins": [{"name": "sentinel"}]})
    settings = _local_settings(project)
    _write_json(
        settings,
        {
            EXTRA_MARKETPLACES_KEY: {"apm": _APM_MARKETPLACE},
            ENABLED_PLUGINS_KEY: {"sentinel@apm": True},
        },
    )
    before = settings.read_bytes()

    assert _sync(project, modules) is False

    assert settings.read_bytes() == before
    assert catalog_path_for(modules).is_file()


def test_user_scope_migrates_the_copilot_home_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copilot_home = tmp_path / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    project, modules = _project(tmp_path)
    _write_legacy(modules, plugins=["sentinel"], settings_path=str(_local_settings(project)))
    user_settings = copilot_home / "settings.json"
    legacy_entries = {
        EXTRA_MARKETPLACES_KEY: {"apm": _APM_MARKETPLACE},
        ENABLED_PLUGINS_KEY: {"sentinel@apm": True},
    }
    _write_json(user_settings, {"theme": "dark", **legacy_entries})
    _write_json(_local_settings(project), legacy_entries)
    decoy_before = _local_settings(project).read_bytes()

    assert _sync(project, modules, scope=InstallScope.USER) is True

    assert read_json(user_settings) == {"theme": "dark"}
    assert _local_settings(project).read_bytes() == decoy_before


def test_dry_run_reports_the_migration_and_changes_nothing(tmp_path: Path) -> None:
    project, modules = _project(tmp_path)
    _write_legacy(modules, plugins=["sentinel"])
    settings = _local_settings(project)
    _write_json(
        settings,
        {
            EXTRA_MARKETPLACES_KEY: {"apm": _APM_MARKETPLACE},
            ENABLED_PLUGINS_KEY: {"sentinel@apm": True},
        },
    )
    tracked = (settings, catalog_path_for(modules), ledger_path_for(modules))
    before = [path.read_bytes() for path in tracked]
    logger = _Logger()

    assert _sync(project, modules, logger=logger, dry_run=True) is True

    assert [path.read_bytes() for path in tracked] == before
    assert len(logger.messages) == 1
    assert str(settings) in logger.messages[0]


def test_registration_status_lists_only_locked_agent_plugins(tmp_path: Path) -> None:
    """Status reads the lock's native records, not any file beside apm_modules."""
    project, modules = _project(tmp_path)
    lock = LockFile()
    for repo, package_type, name in (
        ("acme/portable", "agent_plugin", "portable-plugin"),
        ("acme/claude", "marketplace_plugin", "claude-plugin"),
    ):
        dependency = LockedDependency(
            repo_url=repo, resolved_commit="a" * 40, package_type=package_type
        )
        dependency.native_plugin = NativePluginRecord(
            name, "sha256:" + "0" * 64, "apm-p-00000000", "./.agents/plugins"
        )
        lock.dependencies[repo] = dependency
    lock.dependencies["acme/legacy"] = LockedDependency(
        repo_url="acme/legacy", resolved_commit="b" * 40, package_type="apm_package"
    )
    lock.write(get_lockfile_path(project))

    assert registration_status(modules) == ("portable-plugin",)


def test_registration_status_is_empty_without_a_lock(tmp_path: Path) -> None:
    _, modules = _project(tmp_path)

    assert registration_status(modules) == ()
