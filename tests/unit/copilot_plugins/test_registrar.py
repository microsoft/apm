"""APM-owned Copilot marketplace catalog and settings merge contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.copilot_plugins.capability import NativeRegistrationCapability
from apm_cli.copilot_plugins.catalog import CatalogSourceError, relative_plugin_source
from apm_cli.copilot_plugins.constants import (
    ENABLED_PLUGINS_KEY,
    EXTRA_MARKETPLACES_KEY,
)
from apm_cli.copilot_plugins.registrar import (
    ResolvedPluginCandidate,
    catalog_path_for,
    ledger_path_for,
    registration_status,
    synchronize_copilot_plugins,
)
from apm_cli.copilot_plugins.settings import (
    CopilotSettingsCollisionError,
    read_ledger,
)
from apm_cli.core.scope import InstallScope

from ._builders import read_json, write_agent_plugin

pytestmark = pytest.mark.component

_QUALIFIED = NativeRegistrationCapability(supported=True, target="copilot")
_UNQUALIFIED = NativeRegistrationCapability(
    supported=False, reason="Agent Plugins v1.0.0 packages install natively only for 'copilot'"
)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    modules = project / "apm_modules"
    modules.mkdir(parents=True)
    return project, modules


def _sync(project: Path, modules: Path, candidates, capability=_QUALIFIED, **kwargs):
    return synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=kwargs.pop("scope", InstallScope.PROJECT),
        candidates=candidates,
        capability=capability,
        **kwargs,
    )


def _settings(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.local.json"


def test_direct_and_transitive_plugins_land_in_one_aggregate_catalog(tmp_path: Path) -> None:
    """One catalog per project holds direct and transitive plugins alike."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "acme" / "direct", name="direct-plugin")
    write_agent_plugin(modules / "acme" / "nested" / "transitive", name="transitive-plugin")

    result = _sync(
        project,
        modules,
        [
            ResolvedPluginCandidate("acme/direct", modules / "acme" / "direct"),
            ResolvedPluginCandidate(
                "acme/nested/transitive", modules / "acme" / "nested" / "transitive"
            ),
        ],
    )

    catalog = read_json(catalog_path_for(modules))
    assert result.plugin_names == ("direct-plugin", "transitive-plugin")
    assert catalog["name"] == "apm"
    assert [entry["source"] for entry in catalog["plugins"]] == [
        "./acme/direct",
        "./acme/nested/transitive",
    ]
    assert catalog_path_for(modules) == modules / ".github" / "plugin" / "marketplace.json"


def test_catalog_ordering_is_deterministic_regardless_of_input_order(
    tmp_path: Path,
) -> None:
    """Catalog bytes do not depend on dependency iteration order."""
    project, modules = _project(tmp_path)
    for name in ("zeta", "alpha", "mid"):
        write_agent_plugin(modules / name, name=f"{name}-plugin")
    candidates = [
        ResolvedPluginCandidate(name, modules / name) for name in ("zeta", "alpha", "mid")
    ]

    _sync(project, modules, candidates)
    first = catalog_path_for(modules).read_bytes()
    _sync(project, modules, list(reversed(candidates)))
    second = catalog_path_for(modules).read_bytes()

    assert first == second
    assert [entry["name"] for entry in json.loads(first)["plugins"]] == [
        "alpha-plugin",
        "mid-plugin",
        "zeta-plugin",
    ]


def test_direct_dependency_wins_a_plugin_name_collision_against_a_transitive(
    tmp_path: Path,
) -> None:
    """A directly declared plugin outranks a transitive one claiming its name."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "a-owner" / "pkg", name="shared-plugin", version="1.0.0")
    write_agent_plugin(modules / "z-owner" / "pkg", name="shared-plugin", version="2.0.0")

    result = _sync(
        project,
        modules,
        [
            # The attacker sorts first alphabetically AND is transitive; the
            # direct dependency must still win.
            ResolvedPluginCandidate("a-owner/pkg", modules / "a-owner" / "pkg", direct=False),
            ResolvedPluginCandidate("z-owner/pkg", modules / "z-owner" / "pkg", direct=True),
        ],
    )

    catalog = read_json(catalog_path_for(modules))
    assert result.plugin_names == ("shared-plugin",)
    assert catalog["plugins"][0]["source"] == "./z-owner/pkg"
    assert len(result.collisions) == 1
    assert "z-owner/pkg" in result.collisions[0]
    assert "a-owner/pkg" in result.collisions[0]


def test_same_precedence_plugin_name_collision_is_refused(tmp_path: Path) -> None:
    """Two claimants of one plugin name at the same precedence refuse loudly."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "a-owner" / "pkg", name="shared-plugin", version="1.0.0")
    write_agent_plugin(modules / "b-owner" / "pkg", name="shared-plugin", version="2.0.0")

    with pytest.raises(CopilotSettingsCollisionError, match=r"shared-plugin"):
        _sync(
            project,
            modules,
            [
                ResolvedPluginCandidate("a-owner/pkg", modules / "a-owner" / "pkg", direct=True),
                ResolvedPluginCandidate("b-owner/pkg", modules / "b-owner" / "pkg", direct=True),
            ],
        )
    # The whole registration is refused; nothing is written.
    assert not catalog_path_for(modules).is_file()
    assert not _settings(project).is_file()


def test_advisory_resync_omits_same_precedence_plugin_name_collisions(
    tmp_path: Path,
) -> None:
    """Non-blocking reconciliation still refuses every ambiguous claimant."""
    project, modules = _project(tmp_path)
    for owner in ("a-owner", "b-owner", "c-owner"):
        write_agent_plugin(modules / owner / "pkg", name="shared-plugin")

    result = _sync(
        project,
        modules,
        [
            ResolvedPluginCandidate(
                f"{owner}/pkg",
                modules / owner / "pkg",
                direct=False,
            )
            for owner in ("a-owner", "b-owner", "c-owner")
        ],
        strict=False,
    )

    assert result.entries == []
    assert len(result.collisions) == 2
    assert not catalog_path_for(modules).exists()
    assert not ledger_path_for(modules).exists()
    assert not _settings(project).exists()


def test_legacy_packages_are_not_registered_natively(tmp_path: Path) -> None:
    """A mixed graph registers only exact Agent Plugins."""
    from ._builders import write_legacy_package

    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "native", name="native-plugin")
    write_legacy_package(modules / "legacy", name="legacy-package")

    result = _sync(
        project,
        modules,
        [
            ResolvedPluginCandidate("native", modules / "native"),
            ResolvedPluginCandidate("legacy", modules / "legacy"),
        ],
    )

    assert result.plugin_names == ("native-plugin",)


def test_project_settings_use_a_repository_relative_marketplace_path(
    tmp_path: Path,
) -> None:
    """The registration survives clones and worktrees: no absolute paths."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")

    _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    settings = read_json(_settings(project))
    source = settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"]
    assert source == {"source": "directory", "path": "apm_modules"}
    assert settings[ENABLED_PLUGINS_KEY] == {"portable-plugin@apm": True}


def test_global_scope_records_an_absolute_marketplace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-scope registration is not anchored to any repository."""
    home = tmp_path / "home"
    modules = home / ".apm" / "apm_modules"
    modules.mkdir(parents=True)
    monkeypatch.setenv("COPILOT_HOME", str(home / ".copilot"))
    write_agent_plugin(modules / "pkg", name="global-plugin")

    _sync(
        home,
        modules,
        [ResolvedPluginCandidate("pkg", modules / "pkg")],
        scope=InstallScope.USER,
    )

    settings = read_json(home / ".copilot" / "settings.json")
    assert settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"]["path"] == modules.as_posix()
    assert settings[ENABLED_PLUGINS_KEY] == {"global-plugin@apm": True}


def test_unrelated_user_settings_survive_the_merge(tmp_path: Path) -> None:
    """APM preserves unrelated values while stable serialization may reformat."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    settings_path = _settings(project)
    settings_path.parent.mkdir(parents=True)
    original = (
        '{"banner":"keep me","extraKnownMarketplaces":{"team":{"source":'
        '{"source":"directory","path":"x"}}},"enabledPlugins":'
        '{"team-plugin@team":true}}'
    )
    settings_path.write_text(original, encoding="ascii")

    _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    settings = read_json(settings_path)
    assert settings["banner"] == "keep me"
    assert settings[EXTRA_MARKETPLACES_KEY]["team"] == {
        "source": {"source": "directory", "path": "x"}
    }
    assert settings[ENABLED_PLUGINS_KEY]["team-plugin@team"] is True
    assert settings[ENABLED_PLUGINS_KEY]["portable-plugin@apm"] is True
    assert settings_path.read_text(encoding="ascii") != original


def test_foreign_apm_marketplace_entry_is_a_precise_collision(tmp_path: Path) -> None:
    """APM never overwrites a namespaced entry it does not own."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    settings_path = _settings(project)
    settings_path.parent.mkdir(parents=True)
    original = json.dumps(
        {EXTRA_MARKETPLACES_KEY: {"apm": {"source": {"source": "git", "path": "elsewhere"}}}},
        indent=2,
    )
    settings_path.write_text(original, encoding="ascii")

    with pytest.raises(CopilotSettingsCollisionError, match=r"does not own it"):
        _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    assert settings_path.read_text(encoding="ascii") == original
    assert not catalog_path_for(modules).exists()


def test_foreign_enabled_plugin_key_is_adopted_within_the_apm_namespace(
    tmp_path: Path,
) -> None:
    """APM owns the whole ``@apm`` namespace, so a stray value is corrected."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    settings_path = _settings(project)
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({ENABLED_PLUGINS_KEY: {"portable-plugin@apm": False}}, indent=2),
        encoding="ascii",
    )

    _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    settings = read_json(settings_path)
    assert settings[ENABLED_PLUGINS_KEY]["portable-plugin@apm"] is True


def test_registration_is_rebuilt_when_a_plugin_is_removed(tmp_path: Path) -> None:
    """Uninstall drops only the affected rows and enablement keys."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "kept", name="kept-plugin")
    write_agent_plugin(modules / "gone", name="gone-plugin")
    candidates = [
        ResolvedPluginCandidate("kept", modules / "kept"),
        ResolvedPluginCandidate("gone", modules / "gone"),
    ]
    _sync(project, modules, candidates)

    _sync(project, modules, candidates[:1])

    catalog = read_json(catalog_path_for(modules))
    settings = read_json(_settings(project))
    assert [entry["name"] for entry in catalog["plugins"]] == ["kept-plugin"]
    assert settings[ENABLED_PLUGINS_KEY] == {"kept-plugin@apm": True}
    assert (modules / "gone" / "plugin.json").is_file()


def test_dropped_plugin_is_retired_across_ledger_loss(tmp_path: Path) -> None:
    """A namespace sweep retires a dropped ``@apm`` key even with no ledger.

    Reproduces the ``rm -rf apm_modules`` hazard: the ownership ledger lives
    under the disposable materialization root, so after a wipe the settings
    document still enables a plugin the manifest no longer declares, yet the
    ledger cannot name it. APM owns the whole ``apm`` marketplace namespace by
    construction, so the sweep retires the orphan regardless.
    """
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "alpha", name="alpha")
    write_agent_plugin(modules / "beta", name="beta")
    settings_path = _settings(project)
    _sync(
        project,
        modules,
        [
            ResolvedPluginCandidate("alpha", modules / "alpha"),
            ResolvedPluginCandidate("beta", modules / "beta"),
        ],
    )
    assert read_json(settings_path)[ENABLED_PLUGINS_KEY] == {
        "alpha@apm": True,
        "beta@apm": True,
    }

    # Simulate the ledger loss: the ledger is gone while the settings survive.
    ledger_path_for(modules).unlink()

    _sync(project, modules, [ResolvedPluginCandidate("alpha", modules / "alpha")])

    settings = read_json(settings_path)
    assert settings[ENABLED_PLUGINS_KEY] == {"alpha@apm": True}


def test_namespace_sweep_retires_a_hand_written_apm_key_and_spares_other_namespaces(
    tmp_path: Path,
) -> None:
    """Pin the blast radius of the namespace sweep in both directions.

    APM claims the whole ``apm`` marketplace namespace, so a ``<name>@apm``
    key naming a plugin APM's own catalog does not list is a dangling
    activation and is retired even when a human typed it. That is deliberate
    convergence, not an overwrite of unrelated state: a key in ANY other
    marketplace namespace is untouched. Without this trap the sweep's reach
    is only implied by the ledger-loss test.
    """
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "alpha", name="alpha")
    settings_path = _settings(project)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                ENABLED_PLUGINS_KEY: {
                    "handwritten@apm": True,
                    "vendor-tool@other-marketplace": True,
                },
                "banner": "keep me",
            }
        ),
        encoding="ascii",
    )

    _sync(project, modules, [ResolvedPluginCandidate("alpha", modules / "alpha")])

    settings = read_json(settings_path)
    assert settings[ENABLED_PLUGINS_KEY] == {
        "alpha@apm": True,
        "vendor-tool@other-marketplace": True,
    }
    assert settings["banner"] == "keep me"


def test_emptying_the_graph_removes_the_apm_registration_entirely(tmp_path: Path) -> None:
    """The last plugin leaving takes APM's catalog and settings rows with it."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    settings_path = _settings(project)
    _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    _sync(project, modules, [])

    settings = read_json(settings_path)
    assert EXTRA_MARKETPLACES_KEY not in settings
    assert ENABLED_PLUGINS_KEY not in settings
    assert not catalog_path_for(modules).exists()
    assert not ledger_path_for(modules).exists()
    assert not (modules / ".github").exists()


def test_removal_preserves_unrelated_settings(tmp_path: Path) -> None:
    """Deregistration is surgical, never a settings reset."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    settings_path = _settings(project)
    _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])
    document = read_json(settings_path)
    document["banner"] = "keep me"
    document[ENABLED_PLUGINS_KEY]["team-plugin@team"] = True
    settings_path.write_text(json.dumps(document, indent=2), encoding="ascii")

    _sync(project, modules, [])

    settings = read_json(settings_path)
    assert settings["banner"] == "keep me"
    assert settings[ENABLED_PLUGINS_KEY] == {"team-plugin@team": True}


def test_unqualified_capability_writes_nothing_at_all(tmp_path: Path) -> None:
    """A refused capability leaves the workspace untouched."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")

    result = _sync(
        project,
        modules,
        [ResolvedPluginCandidate("pkg", modules / "pkg")],
        capability=_UNQUALIFIED,
    )

    assert result.entries == []
    assert result.skipped_reason is not None
    assert not catalog_path_for(modules).exists()
    assert not _settings(project).exists()


def test_target_exclusion_retracts_an_existing_registration(tmp_path: Path) -> None:
    """Excluding Copilot retracts APM's rows without inspecting a runtime."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    _sync(
        project,
        modules,
        [ResolvedPluginCandidate("pkg", modules / "pkg")],
        capability=_UNQUALIFIED,
    )

    assert not catalog_path_for(modules).exists()
    assert read_json(_settings(project)) == {}


def test_direct_owner_promotion_write_failure_rolls_back_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed direct-over-transitive promotion leaves the old owner intact."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "transitive", name="shared-plugin")
    _sync(
        project,
        modules,
        [ResolvedPluginCandidate("transitive", modules / "transitive", direct=False)],
    )
    catalog_before = catalog_path_for(modules).read_bytes()
    ledger_before = ledger_path_for(modules).read_bytes()
    settings_before = _settings(project).read_bytes()
    write_agent_plugin(modules / "direct", name="shared-plugin")

    import apm_cli.copilot_plugins.registrar as registrar_mod

    original_atomic = registrar_mod.atomic_write_text

    def _atomic(path, text, **kwargs):
        if path == ledger_path_for(modules):
            raise OSError("ledger write blocked")
        return original_atomic(path, text, **kwargs)

    monkeypatch.setattr(registrar_mod, "atomic_write_text", _atomic)

    with pytest.raises(OSError, match=r"ledger write blocked"):
        _sync(
            project,
            modules,
            [ResolvedPluginCandidate("direct", modules / "direct", direct=True)],
        )

    assert catalog_path_for(modules).read_bytes() == catalog_before
    assert ledger_path_for(modules).read_bytes() == ledger_before
    assert _settings(project).read_bytes() == settings_before


def test_ledger_records_ownership_and_drives_status(tmp_path: Path) -> None:
    """APM proves ownership from its ledger, not from path shape guessing."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")

    _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    ledger = read_ledger(ledger_path_for(modules))
    assert ledger.marketplace_owned is True
    assert ledger.enabled_plugins == ("portable-plugin@apm",)
    assert registration_status(modules) == ("portable-plugin",)


def test_transitive_dependency_cannot_repoint_a_recorded_registration(
    tmp_path: Path,
) -> None:
    """A transitive dep claiming a name the ledger records for another owner refuses."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "z-owner" / "pkg", name="shared-plugin")
    _sync(
        project,
        modules,
        [ResolvedPluginCandidate("z-owner/pkg", modules / "z-owner" / "pkg", direct=True)],
    )
    assert read_ledger(ledger_path_for(modules)).owner_of("shared-plugin@apm") == "z-owner/pkg"

    # A different, transitive dependency now claims the same plugin name. The
    # ledger records z-owner/pkg as the owner, so re-pointing is refused.
    write_agent_plugin(modules / "a-owner" / "pkg", name="shared-plugin")
    with pytest.raises(CopilotSettingsCollisionError, match=r"shared-plugin"):
        _sync(
            project,
            modules,
            [ResolvedPluginCandidate("a-owner/pkg", modules / "a-owner" / "pkg", direct=False)],
        )


def test_advisory_resync_drops_a_transitive_owner_repoint(tmp_path: Path) -> None:
    """Cleanup may continue after a collision but cannot commit the new owner."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "owner", name="shared-plugin")
    _sync(project, modules, [ResolvedPluginCandidate("owner", modules / "owner")])
    write_agent_plugin(modules / "claimant", name="shared-plugin")

    result = _sync(
        project,
        modules,
        [ResolvedPluginCandidate("claimant", modules / "claimant", direct=False)],
        strict=False,
    )

    assert result.entries == []
    assert len(result.collisions) == 1
    assert not catalog_path_for(modules).exists()
    assert not ledger_path_for(modules).exists()
    assert read_json(_settings(project)) == {}


def test_shared_project_settings_are_reused_when_apm_already_owns_them(
    tmp_path: Path,
) -> None:
    """Repository evidence wins over the machine-local default."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    shared = project / ".github" / "copilot" / "settings.json"
    shared.parent.mkdir(parents=True)
    shared.write_text("{}\n", encoding="ascii")
    ledger_path = ledger_path_for(modules)
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "version": 1,
                "marketplace": "apm",
                "marketplaceOwned": True,
                "marketplacePath": "apm_modules",
                "settingsPath": ".github/copilot/settings.json",
                "enabledPlugins": [],
            }
        ),
        encoding="ascii",
    )

    result = _sync(project, modules, [ResolvedPluginCandidate("pkg", modules / "pkg")])

    assert result.settings_path == shared
    assert not _settings(project).exists()
    assert read_json(shared)[ENABLED_PLUGINS_KEY] == {"portable-plugin@apm": True}


def test_plugin_outside_the_marketplace_root_is_refused(tmp_path: Path) -> None:
    """A catalog source must stay inside the APM materialization root."""
    _, modules = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(CatalogSourceError):
        relative_plugin_source(modules, outside)


def test_catalog_lives_strictly_under_the_modules_dir(tmp_path: Path) -> None:
    """The APM catalog path is always inside modules_dir (item 10 boundary)."""
    _, modules = _project(tmp_path)
    catalog = catalog_path_for(modules)

    # relative_to raises if catalog escapes modules_dir; pin it strictly under.
    relative = catalog.relative_to(modules)
    assert relative.parts[0] != ".."
    assert catalog.parent.parent == modules / ".github"


def test_registration_removal_never_climbs_above_the_modules_dir(tmp_path: Path) -> None:
    """The empty-parent rmdir walk stops at modules_dir, never above it."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "acme" / "plugin", name="only-plugin")
    _sync(project, modules, [ResolvedPluginCandidate("acme/plugin", modules / "acme" / "plugin")])
    assert catalog_path_for(modules).is_file()

    # Removing the last registration prunes the empty .github/plugin dirs but
    # must never delete modules_dir or its parent.
    _sync(project, modules, [])

    assert not catalog_path_for(modules).exists()
    assert not (modules / ".github" / "plugin").exists()
    assert modules.is_dir()
    assert modules.parent.is_dir()


def test_prune_empty_parents_never_deletes_the_modules_dir(tmp_path: Path) -> None:
    """The empty-parent walk halts at modules_dir even when it is empty."""
    from apm_cli.copilot_plugins.registrar import _prune_empty_parents

    _, modules = _project(tmp_path)
    catalog = catalog_path_for(modules)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    # Only the (now-empty) generated catalog tree exists under modules_dir.
    _prune_empty_parents(catalog.parent, stop=modules)

    assert modules.is_dir()
    assert modules.parent.is_dir()
    assert not (modules / ".github").exists()


def test_unreadable_ledger_derives_removal_keys_from_the_catalog(tmp_path: Path) -> None:
    """A corrupt ledger on the removal path falls back to catalog-derived keys.

    Fail-OPEN removal (removing nothing while the catalog is deleted) would
    orphan ``enabledPlugins`` entries, so an unreadable ledger is replaced by
    one whose enabled keys are reconstructed from the on-disk catalog.
    """
    from apm_cli.copilot_plugins.registrar import _removal_ledger
    from apm_cli.copilot_plugins.settings import RegistrationLedger

    _, modules = _project(tmp_path)
    catalog = catalog_path_for(modules)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps({"name": "apm", "plugins": [{"name": "sentinel", "source": "./x"}]}),
        encoding="ascii",
    )

    healed = _removal_ledger(RegistrationLedger(unreadable=True), catalog)

    assert healed.marketplace_owned is True
    assert healed.enabled_plugins == ("sentinel@apm",)
